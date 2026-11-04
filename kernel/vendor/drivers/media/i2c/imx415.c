// SPDX-License-Identifier: GPL-2.0
/*
 * imx415 driver
 *
 * Copyright (C) 2020 Rockchip Electronics Co., Ltd.
 *
 * V0.0X01.0X00 first version.
 * V0.0X01.0X01
 *  1. fix hdr ae ratio error,
 *     0x3260 should be set 0x01 in normal mode,
 *     should be 0x00 in hdr mode.
 *  2. rhs1 should be 4n+1 when set hdr ae.
 * V0.0X01.0X02
 *  1. shr0 should be greater than (rsh1 + 9).
 *  2. rhs1 should be ceil to 4n + 1.
 * V0.0X01.0X03
 * V0.0X01.0X04
 * 1. support enum format info by aiq
 * V0.0X01.0X05
 * 1. fixed 10bit hdr2/hdr3 frame rate issue
 * V0.0X01.0X06
 * 1. support DOL3 10bit 20fps 1485Mbps
 * 2. fixed linkfreq error
 */

#define DEBUG
#include <linux/clk.h>
#include <linux/device.h>
#include <linux/delay.h>
#include <linux/gpio/consumer.h>
#include <linux/i2c.h>
#include <linux/module.h>
#include <linux/pm_runtime.h>
#include <linux/sysfs.h>
#include <linux/slab.h>
#include <linux/version.h>
#include <media/media-entity.h>
#include <media/v4l2-async.h>
#include <media/v4l2-ctrls.h>
#include <media/v4l2-subdev.h>

#define DRIVER_VERSION			KERNEL_VERSION(0, 0x01, 0x06)

#ifndef V4L2_CID_DIGITAL_GAIN
#define V4L2_CID_DIGITAL_GAIN		V4L2_CID_GAIN
#endif

#define MIPI_FREQ_891M			891000000
#define MIPI_FREQ_446M			446000000
#define MIPI_FREQ_743M			743000000
#define MIPI_FREQ_297M			297000000

#define IMX415_4LANES			4

#define IMX415_MAX_PIXEL_RATE		(MIPI_FREQ_891M / 10 * 2 * IMX415_4LANES)

#define IMX415_XVCLK_FREQ_37M		37125000

/* TODO: Get the real chip id from reg */
#define CHIP_ID				0xE0
#define IMX415_REG_CHIP_ID		0x311A

#define IMX415_REG_CTRL_MODE		0x3000

#define IMX415_LF_GAIN_REG_H		0x3091
#define IMX415_LF_GAIN_REG_L		0x3090

#define IMX415_LF_EXPO_REG_H		0x3052
#define IMX415_LF_EXPO_REG_M		0x3051
#define IMX415_LF_EXPO_REG_L		0x3050

#define	IMX415_EXPOSURE_MIN		4
#define	IMX415_EXPOSURE_STEP		1
#define IMX415_VTS_MAX			0x7fff

#define IMX415_GAIN_MIN			0x00
#define IMX415_GAIN_MAX			0xf0
#define IMX415_GAIN_STEP		1
#define IMX415_GAIN_DEFAULT		0x00

#define IMX415_FETCH_GAIN_H(VAL)	(((VAL) >> 8) & 0x07)
#define IMX415_FETCH_GAIN_L(VAL)	((VAL) & 0xFF)

#define IMX415_FETCH_EXP_H(VAL)		(((VAL) >> 16) & 0x0F)
#define IMX415_FETCH_EXP_M(VAL)		(((VAL) >> 8) & 0xFF)
#define IMX415_FETCH_EXP_L(VAL)		((VAL) & 0xFF)

#define IMX415_FETCH_VTS_H(VAL)		(((VAL) >> 16) & 0x0F)
#define IMX415_FETCH_VTS_M(VAL)		(((VAL) >> 8) & 0xFF)
#define IMX415_FETCH_VTS_L(VAL)		((VAL) & 0xFF)

#define IMX415_VTS_REG_L		0x3024
#define IMX415_VTS_REG_M		0x3025
#define IMX415_VTS_REG_H		0x3026

#define IMX415_MIRROR_BIT_MASK		BIT(0)
#define IMX415_FLIP_BIT_MASK		BIT(1)
#define IMX415_FLIP_REG			0x3030

#define REG_NULL			0xFFFF

#define IMX415_REG_VALUE_08BIT		1
#define IMX415_REG_VALUE_16BIT		2
#define IMX415_REG_VALUE_24BIT		3

#define IMX415_NAME			"imx415"

struct regval {
	u16 addr;
	u8 val;
};

struct imx415_mode {
	u32 bus_fmt;
	u32 width;
	u32 height;
	struct v4l2_fract max_fps;
	u32 hts_def;
	u32 vts_def;
	u32 exp_def;
	u32 mipi_freq_idx;
	u32 bpp;
	const struct regval *global_reg_list;
	const struct regval *reg_list;
};

struct imx415 {
	struct i2c_client	*client;
	struct clk		*xvclk;
	struct gpio_desc	*reset_gpio;
	struct gpio_desc	*power_gpio;

	struct v4l2_subdev	subdev;
	struct media_pad	pad;
	struct v4l2_ctrl_handler ctrl_handler;
	struct v4l2_ctrl	*exposure;
	struct v4l2_ctrl	*anal_a_gain;
	struct v4l2_ctrl	*digi_gain;
	struct v4l2_ctrl	*hblank;
	struct v4l2_ctrl	*vblank;
	struct v4l2_ctrl	*pixel_rate;
	struct v4l2_ctrl	*link_freq;
	struct mutex		mutex;
	bool			streaming;
	bool			power_on;
	const struct imx415_mode *cur_mode;
	u32			cfg_num;
	u32			cur_vts;
	bool			has_init_exp;
};

#define to_imx415(sd) container_of(sd, struct imx415, subdev)

/*
 * Xclk 37.125Mhz
 */
static const struct regval imx415_global_10bit_regs[] = {
	{0x3002, 0x00},
	{0x3008, 0x7F},
	{0x300A, 0x5B},
	{0x30C1, 0x00},
	{0x3031, 0x00},
	{0x3032, 0x00},
	{0x30D9, 0x06},
	{0x3116, 0x24},
	{0x311E, 0x24},
	{0x32D4, 0x21},
	{0x32EC, 0xA1},
	{0x3452, 0x7F},
	{0x3453, 0x03},
	{0x358A, 0x04},
	{0x35A1, 0x02},
	{0x36BC, 0x0C},
	{0x36CC, 0x53},
	{0x36CD, 0x00},
	{0x36CE, 0x3C},
	{0x36D0, 0x8C},
	{0x36D1, 0x00},
	{0x36D2, 0x71},
	{0x36D4, 0x3C},
	{0x36D6, 0x53},
	{0x36D7, 0x00},
	{0x36D8, 0x71},
	{0x36DA, 0x8C},
	{0x36DB, 0x00},
	{0x3701, 0x00},
	{0x3724, 0x02},
	{0x3726, 0x02},
	{0x3732, 0x02},
	{0x3734, 0x03},
	{0x3736, 0x03},
	{0x3742, 0x03},
	{0x3862, 0xE0},
	{0x38CC, 0x30},
	{0x38CD, 0x2F},
	{0x395C, 0x0C},
	{0x3A42, 0xD1},
	{0x3A4C, 0x77},
	{0x3AE0, 0x02},
	{0x3AEC, 0x0C},
	{0x3B00, 0x2E},
	{0x3B06, 0x29},
	{0x3B98, 0x25},
	{0x3B99, 0x21},
	{0x3B9B, 0x13},
	{0x3B9C, 0x13},
	{0x3B9D, 0x13},
	{0x3B9E, 0x13},
	{0x3BA1, 0x00},
	{0x3BA2, 0x06},
	{0x3BA3, 0x0B},
	{0x3BA4, 0x10},
	{0x3BA5, 0x14},
	{0x3BA6, 0x18},
	{0x3BA7, 0x1A},
	{0x3BA8, 0x1A},
	{0x3BA9, 0x1A},
	{0x3BAC, 0xED},
	{0x3BAD, 0x01},
	{0x3BAE, 0xF6},
	{0x3BAF, 0x02},
	{0x3BB0, 0xA2},
	{0x3BB1, 0x03},
	{0x3BB2, 0xE0},
	{0x3BB3, 0x03},
	{0x3BB4, 0xE0},
	{0x3BB5, 0x03},
	{0x3BB6, 0xE0},
	{0x3BB7, 0x03},
	{0x3BB8, 0xE0},
	{0x3BBA, 0xE0},
	{0x3BBC, 0xDA},
	{0x3BBE, 0x88},
	{0x3BC0, 0x44},
	{0x3BC2, 0x7B},
	{0x3BC4, 0xA2},
	{0x3BC8, 0xBD},
	{0x3BCA, 0xBD},
	{0x4004, 0x48},
	{0x4005, 0x09},
	{REG_NULL, 0x00},
};

static const struct regval imx415_linear_10bit_3840x2160_891M_regs[] = {
	{0x3020, 0x00},
	{0x3021, 0x00},
	{0x3022, 0x00},
	{0x3024, 0xCA},
	{0x3025, 0x08},
	{0x3028, 0x4C},
	{0x3029, 0x04},
	{0x302C, 0x00},
	{0x302D, 0x00},
	{0x3033, 0x05},
	{0x3050, 0x08},
	{0x3051, 0x00},
	{0x3054, 0x19},
	{0x3058, 0x3E},
	{0x3060, 0x25},
	{0x3064, 0x4a},
	{0x30CF, 0x00},
	{0x3118, 0xC0},
	{0x3260, 0x01},
	{0x400C, 0x00},
	{0x4018, 0x7F},
	{0x401A, 0x37},
	{0x401C, 0x37},
	{0x401E, 0xF7},
	{0x401F, 0x00},
	{0x4020, 0x3F},
	{0x4022, 0x6F},
	{0x4024, 0x3F},
	{0x4026, 0x5F},
	{0x4028, 0x2F},
	{0x4074, 0x01},
	{REG_NULL, 0x00},
};

static const struct regval imx415_linear_10bit_1920x1080_594M_regs[] = {
	{0x3020, 0x01},
	{0x3021, 0x01},
	{0x3022, 0x01},
	{0x3024, 0xCA},
	{0x3025, 0x08},
	{0x3028, 0x72},
	{0x3029, 0x06},
	{0x302C, 0x00},
	{0x302D, 0x00},
	{0x3031, 0x00},
	{0x3033, 0x07},
	{0x3050, 0x08},
	{0x3051, 0x00},
	{0x3054, 0x19},
	{0x3058, 0x3E},
	{0x3060, 0x25},
	{0x3064, 0x4A},
	{0x30CF, 0x01},
	{0x30D9, 0x02},
	{0x30DA, 0x01},
	{0x3118, 0x80},
	{0x3260, 0x01},
	{0x3701, 0x00},
	{0x400C, 0x00},
	{0x4018, 0x67},
	{0x401A, 0x27},
	{0x401C, 0x27},
	{0x401E, 0xB7},
	{0x401F, 0x00},
	{0x4020, 0x2F},
	{0x4022, 0x4F},
	{0x4024, 0x2F},
	{0x4026, 0x47},
	{0x4028, 0x27},
	{0x4074, 0x01},
	{REG_NULL, 0x00},
};

/*
 * The width and height must be configured to be
 * the same as the current output resolution of the sensor.
 * The input width of the isp needs to be 16 aligned.
 * The input height of the isp needs to be 8 aligned.
 * If the width or height does not meet the alignment rules,
 * you can configure the cropping parameters with the following function to
 * crop out the appropriate resolution.
 * struct v4l2_subdev_pad_ops {
 *	.get_selection
 * }
 */
static const struct imx415_mode supported_modes[] = {
	/*
	 * frame rate = 1 / (Vtt * 1H) = 1 / (VMAX * 1H)
	 * VMAX >= (PIX_VWIDTH / 2) + 46 = height + 46
	 */
	{
		.bus_fmt = MEDIA_BUS_FMT_SGBRG10_1X10,
		.width = 3840,
		.height = 2160,
		.max_fps = {
			.numerator = 10000,
			.denominator = 300000,
		},
		.exp_def = 0x08ca - 0x08,
		.hts_def = 0x044c * IMX415_4LANES * 2,
		.vts_def = 0x08ca,
		.global_reg_list = imx415_global_10bit_regs,
		.reg_list = imx415_linear_10bit_3840x2160_891M_regs,
		.mipi_freq_idx = 1,
		.bpp = 10,
	},
	{
		.bus_fmt = MEDIA_BUS_FMT_SGBRG10_1X10,
		.width = 1920,
		.height = 1080,
		.max_fps = {
			.numerator = 10000,
			.denominator = 300000,
		},
		.exp_def = 0x05dc - 0x08,
		.hts_def = 0x0672 * 3,
		.vts_def = 0x08ca,
		.global_reg_list = imx415_global_10bit_regs,
		.reg_list = imx415_linear_10bit_1920x1080_594M_regs,
		.mipi_freq_idx = 0,
		.bpp = 10,
	},
};

static const s64 link_freq_items[] = {
	MIPI_FREQ_297M,
	MIPI_FREQ_446M,
	MIPI_FREQ_743M,
	MIPI_FREQ_891M,
};

/* Write registers up to 4 at a time */
static int imx415_write_reg(struct i2c_client *client, u16 reg,
			    u32 len, u32 val)
{
	u32 buf_i, val_i;
	u8 buf[6];
	u8 *val_p;
	__be32 val_be;

	if (len > 4)
		return -EINVAL;

	buf[0] = reg >> 8;
	buf[1] = reg & 0xff;

	val_be = cpu_to_be32(val);
	val_p = (u8 *)&val_be;
	buf_i = 2;
	val_i = 4 - len;

	while (val_i < 4)
		buf[buf_i++] = val_p[val_i++];

	if (i2c_master_send(client, buf, len + 2) != len + 2)
		return -EIO;

	return 0;
}

static int imx415_write_array(struct i2c_client *client,
			      const struct regval *regs)
{
	u32 i;
	int ret = 0;

	for (i = 0; ret == 0 && regs[i].addr != REG_NULL; i++) {
		ret = imx415_write_reg(client, regs[i].addr,
				       IMX415_REG_VALUE_08BIT, regs[i].val);
	}
	return ret;
}

/* Read registers up to 4 at a time */
static int imx415_read_reg(struct i2c_client *client, u16 reg, unsigned int len,
			   u32 *val)
{
	struct i2c_msg msgs[2];
	u8 *data_be_p;
	__be32 data_be = 0;
	__be16 reg_addr_be = cpu_to_be16(reg);
	int ret;

	if (len > 4 || !len)
		return -EINVAL;

	data_be_p = (u8 *)&data_be;
	/* Write register address */
	msgs[0].addr = client->addr;
	msgs[0].flags = 0;
	msgs[0].len = 2;
	msgs[0].buf = (u8 *)&reg_addr_be;

	/* Read data from register */
	msgs[1].addr = client->addr;
	msgs[1].flags = I2C_M_RD;
	msgs[1].len = len;
	msgs[1].buf = &data_be_p[4 - len];

	ret = i2c_transfer(client->adapter, msgs, ARRAY_SIZE(msgs));
	if (ret != ARRAY_SIZE(msgs))
		return -EIO;

	*val = be32_to_cpu(data_be);

	return 0;
}

static int imx415_get_reso_dist(const struct imx415_mode *mode,
				struct v4l2_mbus_framefmt *framefmt)
{
	return abs(mode->width - framefmt->width) +
	       abs(mode->height - framefmt->height);
}

static const struct imx415_mode *
imx415_find_best_fit(struct imx415 *imx415, struct v4l2_subdev_format *fmt)
{
	struct v4l2_mbus_framefmt *framefmt = &fmt->format;
	int dist;
	int cur_best_fit = 0;
	int cur_best_fit_dist = -1;
	unsigned int i;

	for (i = 0; i < imx415->cfg_num; i++) {
		dist = imx415_get_reso_dist(&supported_modes[i], framefmt);
		if ((cur_best_fit_dist == -1 || dist <= cur_best_fit_dist) &&
			supported_modes[i].bus_fmt == framefmt->code) {
			cur_best_fit_dist = dist;
			cur_best_fit = i;
		}
	}

	return &supported_modes[cur_best_fit];
}

static int __imx415_power_on(struct imx415 *imx415);

static void imx415_change_mode(struct imx415 *imx415, const struct imx415_mode *mode)
{
	imx415->cur_mode = mode;
	imx415->cur_vts = imx415->cur_mode->vts_def;
	dev_dbg(&imx415->client->dev, "set fmt: cur_mode: %dx%d\n",
		mode->width, mode->height);
}

static int imx415_set_fmt(struct v4l2_subdev *sd,
			  struct v4l2_subdev_pad_config *cfg,
			  struct v4l2_subdev_format *fmt)
{
	struct imx415 *imx415 = to_imx415(sd);
	const struct imx415_mode *mode;
	s64 h_blank, vblank_def, vblank_min;
	u64 pixel_rate = 0;

	mutex_lock(&imx415->mutex);

	mode = imx415_find_best_fit(imx415, fmt);
	fmt->format.code = mode->bus_fmt;
	fmt->format.width = mode->width;
	fmt->format.height = mode->height;
	fmt->format.field = V4L2_FIELD_NONE;
	if (fmt->which == V4L2_SUBDEV_FORMAT_TRY) {
#ifdef CONFIG_VIDEO_V4L2_SUBDEV_API
		*v4l2_subdev_get_try_format(sd, cfg, fmt->pad) = fmt->format;
#else
		mutex_unlock(&imx415->mutex);
		return -ENOTTY;
#endif
	} else {
		imx415_change_mode(imx415, mode);
		h_blank = mode->hts_def - mode->width;
		__v4l2_ctrl_modify_range(imx415->hblank, h_blank,
					 h_blank, 1, h_blank);
		vblank_def = mode->vts_def - mode->height;
		/* VMAX >= (PIX_VWIDTH / 2) + 46 = height + 46 */
		vblank_min = (mode->height + 46) - mode->height;
		__v4l2_ctrl_modify_range(imx415->vblank, vblank_min,
					 IMX415_VTS_MAX - mode->height,
					 1, vblank_def);
		__v4l2_ctrl_s_ctrl(imx415->link_freq, mode->mipi_freq_idx);
		pixel_rate = (u32)link_freq_items[mode->mipi_freq_idx] / mode->bpp * 2 * IMX415_4LANES;
		__v4l2_ctrl_s_ctrl_int64(imx415->pixel_rate,
					 pixel_rate);
	}

	mutex_unlock(&imx415->mutex);

	return 0;
}

static int imx415_get_fmt(struct v4l2_subdev *sd,
			  struct v4l2_subdev_pad_config *cfg,
			  struct v4l2_subdev_format *fmt)
{
	struct imx415 *imx415 = to_imx415(sd);
	const struct imx415_mode *mode = imx415->cur_mode;

	mutex_lock(&imx415->mutex);
	if (fmt->which == V4L2_SUBDEV_FORMAT_TRY) {
#ifdef CONFIG_VIDEO_V4L2_SUBDEV_API
		fmt->format = *v4l2_subdev_get_try_format(sd, cfg, fmt->pad);
#else
		mutex_unlock(&imx415->mutex);
		return -ENOTTY;
#endif
	} else {
		fmt->format.width = mode->width;
		fmt->format.height = mode->height;
		fmt->format.code = mode->bus_fmt;
		fmt->format.field = V4L2_FIELD_NONE;
		fmt->format.colorspace = V4L2_COLORSPACE_SRGB;
	}
	mutex_unlock(&imx415->mutex);

	return 0;
}

static int imx415_enum_mbus_code(struct v4l2_subdev *sd,
				 struct v4l2_subdev_pad_config *cfg,
				 struct v4l2_subdev_mbus_code_enum *code)
{
	struct imx415 *imx415 = to_imx415(sd);

	if (code->index != 0)
		return -EINVAL;
	code->code = imx415->cur_mode->bus_fmt;

	return 0;
}

static int imx415_enum_frame_sizes(struct v4l2_subdev *sd,
				   struct v4l2_subdev_pad_config *cfg,
				   struct v4l2_subdev_frame_size_enum *fse)
{
	struct imx415 *imx415 = to_imx415(sd);

	if (fse->index >= imx415->cfg_num)
		return -EINVAL;

	if (fse->code != supported_modes[fse->index].bus_fmt)
		return -EINVAL;

	fse->min_width  = supported_modes[fse->index].width;
	fse->max_width  = supported_modes[fse->index].width;
	fse->max_height = supported_modes[fse->index].height;
	fse->min_height = supported_modes[fse->index].height;

	return 0;
}

static int imx415_g_frame_interval(struct v4l2_subdev *sd,
				   struct v4l2_subdev_frame_interval *fi)
{
	struct imx415 *imx415 = to_imx415(sd);
	const struct imx415_mode *mode = imx415->cur_mode;

	mutex_lock(&imx415->mutex);
	fi->interval = mode->max_fps;
	mutex_unlock(&imx415->mutex);

	return 0;
}

static int imx415_g_mbus_config(struct v4l2_subdev *sd,
				struct v4l2_mbus_config *config)
{
	u32 val = 0;

	val = 1 << (IMX415_4LANES - 1) |
	      V4L2_MBUS_CSI2_CHANNEL_0 |
	      V4L2_MBUS_CSI2_CONTINUOUS_CLOCK;
	config->type = V4L2_MBUS_CSI2_DPHY;
	config->flags = val;

	return 0;
}

static int __imx415_start_stream(struct imx415 *imx415)
{
	int ret;

		ret = imx415_write_array(imx415->client, imx415->cur_mode->global_reg_list);
		if (ret)
			return ret;
		ret = imx415_write_array(imx415->client, imx415->cur_mode->reg_list);
		if (ret)
			return ret;

	/* In case these controls are set before streaming */
	ret = __v4l2_ctrl_handler_setup(&imx415->ctrl_handler);
	if (ret)
		return ret;

	return imx415_write_reg(imx415->client, IMX415_REG_CTRL_MODE,
				IMX415_REG_VALUE_08BIT, 0);
}

static int __imx415_stop_stream(struct imx415 *imx415)
{
	imx415->has_init_exp = false;

	return imx415_write_reg(imx415->client, IMX415_REG_CTRL_MODE,
				IMX415_REG_VALUE_08BIT, 1);
}

static int imx415_s_stream(struct v4l2_subdev *sd, int on)
{
	struct imx415 *imx415 = to_imx415(sd);
	struct i2c_client *client = imx415->client;
	int ret = 0;

	dev_dbg(&imx415->client->dev, "s_stream: %d. %dx%d, bpp: %d\n",
	       on, imx415->cur_mode->width, imx415->cur_mode->height,
	       imx415->cur_mode->bpp);

	mutex_lock(&imx415->mutex);
	on = !!on;
	if (on == imx415->streaming)
		goto unlock_and_return;

	if (on) {
		ret = pm_runtime_get_sync(&client->dev);
		if (ret < 0) {
			pm_runtime_put_noidle(&client->dev);
			goto unlock_and_return;
		}

		ret = __imx415_start_stream(imx415);
		if (ret) {
			v4l2_err(sd, "start stream failed while write regs\n");
			pm_runtime_put(&client->dev);
			goto unlock_and_return;
		}
	} else {
		__imx415_stop_stream(imx415);
		pm_runtime_put(&client->dev);
	}

	imx415->streaming = on;

unlock_and_return:
	mutex_unlock(&imx415->mutex);

	return ret;
}

static int imx415_s_power(struct v4l2_subdev *sd, int on)
{
	struct imx415 *imx415 = to_imx415(sd);
	struct i2c_client *client = imx415->client;
	int ret = 0;

	mutex_lock(&imx415->mutex);

	if (imx415->power_on == !!on)
		goto unlock_and_return;

	if (on) {
		ret = pm_runtime_get_sync(&client->dev);
		if (ret < 0) {
			pm_runtime_put_noidle(&client->dev);
			goto unlock_and_return;
		}
		imx415->power_on = true;
	} else {
		pm_runtime_put(&client->dev);
		imx415->power_on = false;
	}

unlock_and_return:
	mutex_unlock(&imx415->mutex);

	return ret;
}

int __imx415_power_on(struct imx415 *imx415)
{
	int ret;
	struct device *dev = &imx415->client->dev;

	if (!IS_ERR(imx415->power_gpio))
		gpiod_direction_output(imx415->power_gpio, 1);
	/* At least 500ns between power raising and XCLR */
	/* fix power on timing if insmod this ko */
	usleep_range(10 * 1000, 20 * 1000);
	if (!IS_ERR(imx415->reset_gpio))
		gpiod_direction_output(imx415->reset_gpio, 0);

	/* At least 1us between XCLR and clk */
	/* fix power on timing if insmod this ko */
	usleep_range(10 * 1000, 20 * 1000);
	ret = clk_set_rate(imx415->xvclk, IMX415_XVCLK_FREQ_37M);
	if (ret < 0)
		dev_warn(dev, "Failed to set xvclk rate\n");
	if (clk_get_rate(imx415->xvclk) != IMX415_XVCLK_FREQ_37M)
		dev_warn(dev, "xvclk mismatched\n");
	ret = clk_prepare_enable(imx415->xvclk);
	if (ret < 0) {
		dev_err(dev, "Failed to enable xvclk\n");
		goto err_clk;
	}

	/* At least 20us between XCLR and I2C communication */
	usleep_range(20*1000, 30*1000);

	return 0;

err_clk:
	if (!IS_ERR(imx415->reset_gpio))
		gpiod_direction_output(imx415->reset_gpio, 1);

	return ret;
}

static void __imx415_power_off(struct imx415 *imx415)
{
	if (!IS_ERR(imx415->reset_gpio))
		gpiod_direction_output(imx415->reset_gpio, 1);
	clk_disable_unprepare(imx415->xvclk);

	if (!IS_ERR(imx415->power_gpio))
		gpiod_direction_output(imx415->power_gpio, 0);
}

static int imx415_runtime_resume(struct device *dev)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct v4l2_subdev *sd = i2c_get_clientdata(client);
	struct imx415 *imx415 = to_imx415(sd);

	return __imx415_power_on(imx415);
}

static int imx415_runtime_suspend(struct device *dev)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct v4l2_subdev *sd = i2c_get_clientdata(client);
	struct imx415 *imx415 = to_imx415(sd);

	__imx415_power_off(imx415);

	return 0;
}

#ifdef CONFIG_VIDEO_V4L2_SUBDEV_API
static int imx415_open(struct v4l2_subdev *sd, struct v4l2_subdev_fh *fh)
{
	struct imx415 *imx415 = to_imx415(sd);
	struct v4l2_mbus_framefmt *try_fmt =
				v4l2_subdev_get_try_format(sd, fh->pad, 0);
	const struct imx415_mode *def_mode = &supported_modes[0];

	mutex_lock(&imx415->mutex);
	/* Initialize try_fmt */
	try_fmt->width = def_mode->width;
	try_fmt->height = def_mode->height;
	try_fmt->code = def_mode->bus_fmt;
	try_fmt->field = V4L2_FIELD_NONE;

	mutex_unlock(&imx415->mutex);
	/* No crop or compose */

	return 0;
}
#endif

static int imx415_enum_frame_interval(struct v4l2_subdev *sd,
	struct v4l2_subdev_pad_config *cfg,
	struct v4l2_subdev_frame_interval_enum *fie)
{
	struct imx415 *imx415 = to_imx415(sd);

	if (fie->index >= imx415->cfg_num)
		return -EINVAL;

	fie->code = supported_modes[fie->index].bus_fmt;
	fie->width = supported_modes[fie->index].width;
	fie->height = supported_modes[fie->index].height;
	fie->interval = supported_modes[fie->index].max_fps;
	return 0;
}

#define CROP_START(SRC, DST) (((SRC) - (DST)) / 2 / 4 * 4)
#define DST_WIDTH_3840 3840
#define DST_HEIGHT_2160 2160
#define DST_WIDTH_1920 1920
#define DST_HEIGHT_1080 1080

/*
 * The resolution of the driver configuration needs to be exactly
 * the same as the current output resolution of the sensor,
 * the input width of the isp needs to be 16 aligned,
 * the input height of the isp needs to be 8 aligned.
 * Can be cropped to standard resolution by this function,
 * otherwise it will crop out strange resolution according
 * to the alignment rules.
 */
static int imx415_get_selection(struct v4l2_subdev *sd,
				struct v4l2_subdev_pad_config *cfg,
				struct v4l2_subdev_selection *sel)
{
	struct imx415 *imx415 = to_imx415(sd);

	if (sel->target == V4L2_SEL_TGT_CROP_BOUNDS) {
		if (imx415->cur_mode->width == 3840) {
			sel->r.left = CROP_START(imx415->cur_mode->width, DST_WIDTH_3840);
			sel->r.width = DST_WIDTH_3840;
			sel->r.top = CROP_START(imx415->cur_mode->height, DST_HEIGHT_2160);
			sel->r.height = DST_HEIGHT_2160;
		} else if (imx415->cur_mode->width == 1920) {
			sel->r.left = CROP_START(imx415->cur_mode->width, DST_WIDTH_1920);
			sel->r.width = DST_WIDTH_1920;
			sel->r.top = CROP_START(imx415->cur_mode->height, DST_HEIGHT_1080);
			sel->r.height = DST_HEIGHT_1080;
		} else {
			sel->r.left = CROP_START(imx415->cur_mode->width, imx415->cur_mode->width);
			sel->r.width = imx415->cur_mode->width;
			sel->r.top = CROP_START(imx415->cur_mode->height, imx415->cur_mode->height);
			sel->r.height = imx415->cur_mode->height;
		}
		return 0;
	}
	return -EINVAL;
}

static const struct dev_pm_ops imx415_pm_ops = {
	SET_RUNTIME_PM_OPS(imx415_runtime_suspend,
			   imx415_runtime_resume, NULL)
};

#ifdef CONFIG_VIDEO_V4L2_SUBDEV_API
static const struct v4l2_subdev_internal_ops imx415_internal_ops = {
	.open = imx415_open,
};
#endif

static const struct v4l2_subdev_core_ops imx415_core_ops = {
	.s_power = imx415_s_power,
};

static const struct v4l2_subdev_video_ops imx415_video_ops = {
	.s_stream = imx415_s_stream,
	.g_frame_interval = imx415_g_frame_interval,
	.g_mbus_config = imx415_g_mbus_config,
};

static const struct v4l2_subdev_pad_ops imx415_pad_ops = {
	.enum_mbus_code = imx415_enum_mbus_code,
	.enum_frame_size = imx415_enum_frame_sizes,
	.enum_frame_interval = imx415_enum_frame_interval,
	.get_fmt = imx415_get_fmt,
	.set_fmt = imx415_set_fmt,
	.get_selection = imx415_get_selection,
};

static const struct v4l2_subdev_ops imx415_subdev_ops = {
	.core	= &imx415_core_ops,
	.video	= &imx415_video_ops,
	.pad	= &imx415_pad_ops,
};

static int imx415_set_ctrl(struct v4l2_ctrl *ctrl)
{
	struct imx415 *imx415 = container_of(ctrl->handler,
					     struct imx415, ctrl_handler);
	struct i2c_client *client = imx415->client;
	s64 max;
	u32 vts = 0, val;
	int ret = 0;
	u32 shr0 = 0;

	/* Propagate change of current control to all related controls */
	switch (ctrl->id) {
	case V4L2_CID_VBLANK:
			/* Update max exposure while meeting expected vblanking */
			max = imx415->cur_mode->height + ctrl->val - 8;
			__v4l2_ctrl_modify_range(imx415->exposure,
					 imx415->exposure->minimum, max,
					 imx415->exposure->step,
					 imx415->exposure->default_value);
		break;
	}

	if (!pm_runtime_get_if_in_use(&client->dev))
		return 0;

	switch (ctrl->id) {
	case V4L2_CID_EXPOSURE:
		shr0 = imx415->cur_vts - ctrl->val;
		ret = imx415_write_reg(imx415->client, IMX415_LF_EXPO_REG_L,
				       IMX415_REG_VALUE_08BIT,
				       IMX415_FETCH_EXP_L(shr0));
		ret |= imx415_write_reg(imx415->client, IMX415_LF_EXPO_REG_M,
				       IMX415_REG_VALUE_08BIT,
				       IMX415_FETCH_EXP_M(shr0));
		ret |= imx415_write_reg(imx415->client, IMX415_LF_EXPO_REG_H,
				       IMX415_REG_VALUE_08BIT,
				       IMX415_FETCH_EXP_H(shr0));
		dev_dbg(&client->dev, "set exposure(shr0) %d = cur_vts(%d) - val(%d)\n",
			shr0, imx415->cur_vts, ctrl->val);
		break;
	case V4L2_CID_ANALOGUE_GAIN:
		ret = imx415_write_reg(imx415->client, IMX415_LF_GAIN_REG_H,
				       IMX415_REG_VALUE_08BIT,
				       IMX415_FETCH_GAIN_H(ctrl->val));
		ret |= imx415_write_reg(imx415->client, IMX415_LF_GAIN_REG_L,
				       IMX415_REG_VALUE_08BIT,
				       IMX415_FETCH_GAIN_L(ctrl->val));
		dev_dbg(&client->dev, "set analog gain 0x%x\n",
			ctrl->val);
		break;
	case V4L2_CID_VBLANK:
		vts = ctrl->val + imx415->cur_mode->height;
		imx415->cur_vts = vts;
		ret = imx415_write_reg(imx415->client, IMX415_VTS_REG_L,
				       IMX415_REG_VALUE_08BIT,
				       IMX415_FETCH_VTS_L(vts));
		ret |= imx415_write_reg(imx415->client, IMX415_VTS_REG_M,
				       IMX415_REG_VALUE_08BIT,
				       IMX415_FETCH_VTS_M(vts));
		ret |= imx415_write_reg(imx415->client, IMX415_VTS_REG_H,
				       IMX415_REG_VALUE_08BIT,
				       IMX415_FETCH_VTS_H(vts));
		dev_dbg(&client->dev, "set vblank 0x%x vts %d\n",
			ctrl->val, vts);
		break;
	case V4L2_CID_HFLIP:
		ret = imx415_read_reg(imx415->client, IMX415_FLIP_REG,
				      IMX415_REG_VALUE_08BIT, &val);
		if (ret)
			break;
		if (ctrl->val)
			val |= IMX415_MIRROR_BIT_MASK;
		else
			val &= ~IMX415_MIRROR_BIT_MASK;
		ret = imx415_write_reg(imx415->client, IMX415_FLIP_REG,
				       IMX415_REG_VALUE_08BIT, val);
		break;
	case V4L2_CID_VFLIP:
		ret = imx415_read_reg(imx415->client, IMX415_FLIP_REG,
				      IMX415_REG_VALUE_08BIT, &val);
		if (ret)
			break;
		if (ctrl->val)
			val |= IMX415_FLIP_BIT_MASK;
		else
			val &= ~IMX415_FLIP_BIT_MASK;
		ret = imx415_write_reg(imx415->client, IMX415_FLIP_REG,
				       IMX415_REG_VALUE_08BIT, val);
		break;
	default:
		dev_warn(&client->dev, "%s Unhandled id:0x%x, val:0x%x\n",
			 __func__, ctrl->id, ctrl->val);
		break;
	}

	pm_runtime_put(&client->dev);

	return ret;
}

static const struct v4l2_ctrl_ops imx415_ctrl_ops = {
	.s_ctrl = imx415_set_ctrl,
};

static int imx415_initialize_controls(struct imx415 *imx415)
{
	const struct imx415_mode *mode;
	struct v4l2_ctrl_handler *handler;
	s64 exposure_max, vblank_def;
	u64 pixel_rate;
	u32 h_blank;
	int ret;

	handler = &imx415->ctrl_handler;
	mode = imx415->cur_mode;
	ret = v4l2_ctrl_handler_init(handler, 8);
	if (ret)
		return ret;
	handler->lock = &imx415->mutex;

	imx415->link_freq = v4l2_ctrl_new_int_menu(handler, NULL,
				V4L2_CID_LINK_FREQ,
				ARRAY_SIZE(link_freq_items) - 1, 0,
				link_freq_items);
	__v4l2_ctrl_s_ctrl(imx415->link_freq, mode->mipi_freq_idx);

	/* pixel rate = link frequency * 2 * lanes / BITS_PER_SAMPLE */
	pixel_rate = (u32)link_freq_items[mode->mipi_freq_idx] / mode->bpp * 2 * IMX415_4LANES;
	imx415->pixel_rate = v4l2_ctrl_new_std(handler, NULL,
		V4L2_CID_PIXEL_RATE, 0, IMX415_MAX_PIXEL_RATE,
		1, pixel_rate);

	h_blank = mode->hts_def - mode->width;
	imx415->hblank = v4l2_ctrl_new_std(handler, NULL, V4L2_CID_HBLANK,
				h_blank, h_blank, 1, h_blank);
	if (imx415->hblank)
		imx415->hblank->flags |= V4L2_CTRL_FLAG_READ_ONLY;

	vblank_def = mode->vts_def - mode->height;
	imx415->vblank = v4l2_ctrl_new_std(handler, &imx415_ctrl_ops,
				V4L2_CID_VBLANK, vblank_def,
				IMX415_VTS_MAX - mode->height,
				1, vblank_def);
	imx415->cur_vts = mode->vts_def;

	exposure_max = mode->vts_def - 8;
	imx415->exposure = v4l2_ctrl_new_std(handler, &imx415_ctrl_ops,
				V4L2_CID_EXPOSURE, IMX415_EXPOSURE_MIN,
				exposure_max, IMX415_EXPOSURE_STEP,
				mode->exp_def);

	imx415->anal_a_gain = v4l2_ctrl_new_std(handler, &imx415_ctrl_ops,
				V4L2_CID_ANALOGUE_GAIN, IMX415_GAIN_MIN,
				IMX415_GAIN_MAX, IMX415_GAIN_STEP,
				IMX415_GAIN_DEFAULT);

	v4l2_ctrl_new_std(handler, &imx415_ctrl_ops, V4L2_CID_HFLIP, 0, 1, 1, 0);
	v4l2_ctrl_new_std(handler, &imx415_ctrl_ops, V4L2_CID_VFLIP, 0, 1, 1, 0);

	if (handler->error) {
		ret = handler->error;
		dev_err(&imx415->client->dev,
			"Failed to init controls(%d)\n", ret);
		goto err_free_handler;
	}

	imx415->subdev.ctrl_handler = handler;
	imx415->has_init_exp = false;

	return 0;

err_free_handler:
	v4l2_ctrl_handler_free(handler);

	return ret;
}

static int imx415_check_sensor_id(struct imx415 *imx415,
				  struct i2c_client *client)
{
	struct device *dev = &imx415->client->dev;
	u32 id = 0;
	int ret;

	ret = imx415_read_reg(client, IMX415_REG_CHIP_ID,
			      IMX415_REG_VALUE_08BIT, &id);
	if (id != CHIP_ID) {
		dev_err(dev, "Unexpected sensor id(%06x), ret(%d)\n", id, ret);
		return -ENODEV;
	}

	dev_info(dev, "Detected imx415 id %06x\n", CHIP_ID);

	return 0;
}

static int imx415_probe(struct i2c_client *client,
			const struct i2c_device_id *id)
{
	struct device *dev = &client->dev;
	struct imx415 *imx415;
	struct v4l2_subdev *sd;
	int ret;

	dev_info(dev, "driver version: %02x.%02x.%02x",
		DRIVER_VERSION >> 16,
		(DRIVER_VERSION & 0xff00) >> 8,
		DRIVER_VERSION & 0x00ff);

	imx415 = devm_kzalloc(dev, sizeof(*imx415), GFP_KERNEL);
	if (!imx415)
		return -ENOMEM;

	imx415->client = client;
	imx415->cfg_num = ARRAY_SIZE(supported_modes);

	imx415->cur_mode = &supported_modes[0];

	imx415->xvclk = devm_clk_get(dev, "xvclk");
	if (IS_ERR(imx415->xvclk)) {
		dev_err(dev, "Failed to get xvclk\n");
		return -EINVAL;
	}

	imx415->reset_gpio = devm_gpiod_get(dev, "reset", GPIOD_OUT_LOW);
	if (IS_ERR(imx415->reset_gpio))
		dev_warn(dev, "Failed to get reset-gpios\n");
	imx415->power_gpio = devm_gpiod_get(dev, "pwdn", GPIOD_OUT_HIGH);
	if (IS_ERR(imx415->power_gpio))
		dev_warn(dev, "Failed to get power-gpios\n");

	mutex_init(&imx415->mutex);

	sd = &imx415->subdev;
	v4l2_i2c_subdev_init(sd, client, &imx415_subdev_ops);
	ret = imx415_initialize_controls(imx415);
	if (ret)
		goto err_destroy_mutex;

	ret = __imx415_power_on(imx415);
	if (ret)
		goto err_free_handler;

	ret = imx415_check_sensor_id(imx415, client);
	if (ret)
		goto err_power_off;

#ifdef CONFIG_VIDEO_V4L2_SUBDEV_API
	sd->internal_ops = &imx415_internal_ops;
	sd->flags |= V4L2_SUBDEV_FL_HAS_DEVNODE |
		     V4L2_SUBDEV_FL_HAS_EVENTS;
#endif
#if defined(CONFIG_MEDIA_CONTROLLER)
	imx415->pad.flags = MEDIA_PAD_FL_SOURCE;
	sd->entity.function = MEDIA_ENT_F_CAM_SENSOR;
	ret = media_entity_pads_init(&sd->entity, 1, &imx415->pad);
	if (ret < 0)
		goto err_power_off;
#endif

	snprintf(sd->name, sizeof(sd->name), "m%s %s",
		 IMX415_NAME, dev_name(sd->dev));
	ret = v4l2_async_register_subdev_sensor_common(sd);
	if (ret) {
		dev_err(dev, "v4l2 async register subdev failed\n");
		goto err_clean_entity;
	}

	pm_runtime_set_active(dev);
	pm_runtime_enable(dev);
	pm_runtime_idle(dev);

	return 0;

err_clean_entity:
#if defined(CONFIG_MEDIA_CONTROLLER)
	media_entity_cleanup(&sd->entity);
#endif
err_power_off:
	__imx415_power_off(imx415);
err_free_handler:
	v4l2_ctrl_handler_free(&imx415->ctrl_handler);
err_destroy_mutex:
	mutex_destroy(&imx415->mutex);

	return ret;
}

static int imx415_remove(struct i2c_client *client)
{
	struct v4l2_subdev *sd = i2c_get_clientdata(client);
	struct imx415 *imx415 = to_imx415(sd);

	v4l2_async_unregister_subdev(sd);
#if defined(CONFIG_MEDIA_CONTROLLER)
	media_entity_cleanup(&sd->entity);
#endif
	v4l2_ctrl_handler_free(&imx415->ctrl_handler);
	mutex_destroy(&imx415->mutex);

	pm_runtime_disable(&client->dev);
	if (!pm_runtime_status_suspended(&client->dev))
		__imx415_power_off(imx415);
	pm_runtime_set_suspended(&client->dev);

	return 0;
}

#if IS_ENABLED(CONFIG_OF)
static const struct of_device_id imx415_of_match[] = {
	{ .compatible = "sony,imx415" },
	{},
};
MODULE_DEVICE_TABLE(of, imx415_of_match);
#endif

static const struct i2c_device_id imx415_match_id[] = {
	{ "sony,imx415", 0 },
	{ },
};

static struct i2c_driver imx415_i2c_driver = {
	.driver = {
		.name = IMX415_NAME,
		.pm = &imx415_pm_ops,
		.of_match_table = of_match_ptr(imx415_of_match),
	},
	.probe		= &imx415_probe,
	.remove		= &imx415_remove,
	.id_table	= imx415_match_id,
};

static int __init sensor_mod_init(void)
{
	return i2c_add_driver(&imx415_i2c_driver);
}

static void __exit sensor_mod_exit(void)
{
	i2c_del_driver(&imx415_i2c_driver);
}

device_initcall_sync(sensor_mod_init);
module_exit(sensor_mod_exit);

MODULE_DESCRIPTION("Sony imx415 sensor driver");
MODULE_LICENSE("GPL v2");
