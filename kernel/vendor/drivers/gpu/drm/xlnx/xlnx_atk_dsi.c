// SPDX-License-Identifier: GPL-2.0
/*
 * Xilinx FPGA atk LCD Tx Subsystem driver.
 *
 * Copyright (c) 2023 Xilinx Pvt., Ltd
 *
 * Authors:
 * Alientek
 */

#include <linux/clk.h>
#include <linux/pwm.h>
#include <linux/component.h>
#include <linux/device.h>
#include <linux/of_device.h>
#include <linux/of_graph.h>
#include <linux/of_gpio.h>
#include <video/videomode.h>
#include <video/of_videomode.h>

#include <drm/drmP.h>
#include <drm/drm_of.h>
#include <drm/drm_crtc_helper.h>
#include <drm/drm_atomic_helper.h>
#include <drm/drm_connector.h>
#include <drm/drm_probe_helper.h>
#include <drm/drm_print.h>

/* 正点原子MIPI LCD屏硬件ID */
#define ATK_MIPI_720P	1		//（1） 5.5寸720*1280
#define ATK_MIPI_1080P	2		//（2） 5.5寸1080*1920

/**
 * struct atk_dpi - Core configuration atk Tx subsystem device structure
 * @encoder: DRM encoder structure
 * @connector: DRM connector structure
 * @dev: device structure
 * @vm: videomode structure
 * @pclk: Video Clock
 */
struct atk_dpi {
	struct device *dev;
	struct drm_encoder encoder;
	struct drm_connector connector;

	struct videomode *vm;
	struct clk *pclk;
};

#define connector_to_dpi(c) container_of(c, struct atk_dpi, connector)
#define encoder_to_dpi(e) container_of(e, struct atk_dpi, encoder)

/* Global variables */
unsigned dsi_id;

static enum drm_connector_status
atk_dpi_detect(struct drm_connector *connector, bool force)
{
	return connector_status_connected;
}

static void atk_dpi_connector_destroy(struct drm_connector *connector)
{
	drm_connector_unregister(connector);
	drm_connector_cleanup(connector);
}

static const struct drm_connector_funcs atk_dpi_connector_funcs = {
	.detect = atk_dpi_detect,
	.fill_modes = drm_helper_probe_single_connector_modes,
	.reset = drm_atomic_helper_connector_reset,
	.destroy = atk_dpi_connector_destroy,
	.atomic_duplicate_state	= drm_atomic_helper_connector_duplicate_state,
	.atomic_destroy_state = drm_atomic_helper_connector_destroy_state,
};

static int lcd_get_modes(struct drm_connector *connector)
{
	struct atk_dpi *dpi = connector_to_dpi(connector);

	if (dpi->vm) {
		struct drm_display_mode *mode;

		mode = drm_mode_create(connector->dev);
		if (!mode) {
			DRM_DEV_ERROR(dpi->dev,
				      "failed to create a new display mode\n");
			return 0;
		}
		drm_display_mode_from_videomode(dpi->vm, mode);
		drm_mode_set_name(mode);
		mode->type = DRM_MODE_TYPE_DRIVER | DRM_MODE_TYPE_PREFERRED;
		drm_mode_probed_add(connector, mode);
		return 1;
	}

	return 0;
}

static int lcd_mode_valid(struct drm_connector *connector,
			       struct drm_display_mode *mode)
{
	return MODE_OK;
}

static struct drm_connector_helper_funcs atk_dpi_connector_helper_funcs = {
	.get_modes = lcd_get_modes,
	.mode_valid = lcd_mode_valid,
};

static void atk_dpi_mode_set(struct drm_encoder *encoder,
				struct drm_display_mode *mode,
				struct drm_display_mode *adjusted_mode)
{
	// struct atk_dpi *dpi = encoder_to_dpi(encoder);
	// long unsigned int pclk_rate;
	// int ret;

	// pclk_rate =  mode->clock * 1000;
	// ret = clk_set_rate(dpi->pclk,pclk_rate);

	// if (ret)
	// 	DRM_DEV_ERROR(dpi->dev, "failed to set clk rate = %lu\n",
	// 		pclk_rate);
}

static void atk_dpi_enable(struct drm_encoder *encoder)
{
	  struct atk_dpi *dpi = encoder_to_dpi(encoder);

	 if (clk_prepare_enable(dpi->pclk))
	 	DRM_DEV_ERROR(dpi->dev,
	 		      "Cannot enable lcd pclk clock!\n");
}

static void atk_dpi_disable(struct drm_encoder *encoder)
{
	struct atk_dpi *dpi = encoder_to_dpi(encoder);

	clk_disable_unprepare(dpi->pclk);
}

static const struct drm_encoder_helper_funcs atk_dpi_encoder_helper_funcs = {
	.mode_set	= atk_dpi_mode_set,
	.enable = atk_dpi_enable,
	.disable = atk_dpi_disable,
};

static const struct drm_encoder_funcs atk_dpi_encoder_funcs = {
	.destroy = drm_encoder_cleanup,
};

static int atk_dpi_bind(struct device *dev, struct device *master,
			 void *data)
{
	struct atk_dpi *dpi = dev_get_drvdata(dev);
	struct drm_encoder *encoder = &dpi->encoder;
	struct drm_device *drm_dev = data;
	struct device_node *port;
	int ret;

	/*
	 * TODO: The possible CRTCs are 1 now as per current implementation of
	 * atk tx drivers. DRM framework can support more than one CRTCs and
	 * atk driver can be enhanced for that.
	 */
	encoder->possible_crtcs = 1;

	for_each_child_of_node(dev->of_node, port) {
		if (!port->name || of_node_cmp(port->name, "ports")) {
			DRM_DEBUG_DRIVER("port name is null or node name is not ports!\n");
			continue;
		}
		encoder->possible_crtcs |= drm_of_find_possible_crtcs(drm_dev, port);
	}

	/* initialize encoder */
	drm_encoder_init(drm_dev, encoder, &atk_dpi_encoder_funcs,
			 DRM_MODE_ENCODER_DPI, NULL);
	drm_encoder_helper_add(encoder, &atk_dpi_encoder_helper_funcs);

	/* initialize connector */
	ret = drm_connector_init(drm_dev, &dpi->connector,
				 &atk_dpi_connector_funcs, DRM_MODE_CONNECTOR_DPI);
	if (ret) {
		DRM_DEV_ERROR(dpi->dev, "Failed to initialize connector with drm\n");
		drm_encoder_cleanup(encoder);
		return ret;
	}

	drm_connector_helper_add(&dpi->connector, &atk_dpi_connector_helper_funcs);
	drm_connector_register(&dpi->connector);
	drm_connector_attach_encoder(&dpi->connector, encoder);

	return ret;
}

static void atk_dpi_unbind(struct device *dev, struct device *master,
			    void *data)
{
	struct atk_dpi *dpi = dev_get_drvdata(dev);

	drm_encoder_cleanup(&dpi->encoder);
	drm_connector_cleanup(&dpi->connector);
}

static const struct component_ops atk_dpi_component_ops = {
	.bind	= atk_dpi_bind,
	.unbind	= atk_dpi_unbind,
};

static int atkencoder_init_dt(struct atk_dpi *dpi)
{
	struct device *dev = dpi->dev;
	struct device_node *dn = dev->of_node;
	struct device_node *np;
	int display_timing;
	int gpios[2];
	int ret, i;

	/* 读取LCD屏ID */
	for (i = 0; i < 2; i++) {
		gpios[i] = of_get_named_gpio(dev->of_node, "atk,dsi_id", i);
		if (!gpio_is_valid(gpios[i])) {
			DRM_DEV_ERROR(dev, "Failed to get lcd id gpio : %d\n", gpios[i]);
			dsi_id = ATK_MIPI_720P; // 设置为默认 LCD 屏 5.5 寸 720P
			break;
		}

		ret = devm_gpio_request_one(dev, gpios[i], GPIOF_IN, "lcd hardware ID");
		if (ret < 0) {
			DRM_DEV_ERROR(dev, "Failed to request lcd id gpio\n");
			dsi_id = ATK_MIPI_720P; //设置为默认 LCD 屏 5.5 寸 720P
			break;
	 	}

		 dsi_id |= (gpio_get_value_cansleep(gpios[i]) << i); // 读取 GPIO 数值
	}

	/* 根据LCD ID匹配对应的时序参数 */
	switch (dsi_id) {
	case ATK_MIPI_720P: display_timing = 0;
	        dev_info(dev, "Alientek MIPI LCD ID: %d,resolution: 720P\n", dsi_id);
			break;
	case ATK_MIPI_1080P: display_timing = 1; 
	        dev_info(dev, "Alientek MIPI LCD ID: %d,resolution: 1080P\n", dsi_id);
			break;
	default: display_timing = 0;
			dev_info(dev, "MIPI LCD ID Match failed, using default configurationz : 720P\n");
			break;
	}

	np = of_get_child_by_name(dn, "display-timings");
	if (np) {
		struct videomode *vm;
		int ret;
		of_node_put(np);

		vm = devm_kzalloc(dev, sizeof(*dpi->vm), GFP_KERNEL);
		if (!vm)
			return -ENOMEM;

		ret = of_get_videomode(dn, vm, display_timing);
		if (ret < 0) {
			DRM_DEV_ERROR(dev, "Failed to get videomode from DT\n");
			devm_kfree(dev, vm);
			return ret;
		}

		dpi->vm = vm;

		dev_info(dpi->dev, "MIPI LCD Size = %dx%d\n", dpi->vm->hactive, dpi->vm->vactive);

		return 0;
	}

	dev_info(dev, "atkfb_init_fbinfo_dt finished\n");

	return 0;
}

static int atk_dpi_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct atk_dpi *dpi;
	struct pwm_device *pwm;
	int ret;

	dpi = devm_kzalloc(dev, sizeof(*dpi), GFP_KERNEL);
	if (!dpi)
		return -ENOMEM;

	dpi->dev = dev;

	dpi->pclk = devm_clk_get(dev, "s_axis_aclk");
	if (IS_ERR(dpi->pclk)) {
		ret = PTR_ERR(dpi->pclk);
		DRM_DEV_ERROR(dev, "failed to get lcd_pclk %d\n", ret);
		return ret;
	}

	// clk_disable_unprepare(dpi->pclk);		// 先禁止时钟输出



//	clk_set_rate(dpi->pclk, dpi->vm->pixelclock);
	ret = clk_prepare_enable(dpi->pclk);
	if (ret) {
		DRM_DEV_ERROR(dev, "failed to enable pclk %d\n", ret);
		goto out1;
	}

	/* 打开LCD背光 */
	pwm = devm_pwm_get(dev, NULL);
	if (IS_ERR(pwm)) {
		DRM_DEV_ERROR(dev, "failed devm_pwm_get- %d\n", ret);
		goto out1;
	}
	else {
		pwm_config(pwm, 5000000, 5000000);	// 配置PWM
		pwm_enable(pwm);		// 使能PWM打开背光
		pwm_free(pwm);
	}

	platform_set_drvdata(pdev, dpi);

		/* 初始化info变量 */
	ret = atkencoder_init_dt(dpi);
	if (ret < 0) {
		devm_kfree(dev, dpi);
		goto out1;
	}

	ret = component_add(dev, &atk_dpi_component_ops);
	if (ret < 0){
		DRM_DEV_ERROR(dev, "failed component_add- %d\n", ret);
		// goto err_disable_pclk;
	}

	dev_info(&pdev->dev, "Atk encoder driver probed\n");
	return ret;

// err_disable_pclk:
// 	clk_disable_unprepare(dpi->pclk);

out1:
	printk("atk lcd failed\n");

	return ret;
}

static int atk_dpi_remove(struct platform_device *pdev)
{
	struct atk_dpi *dpi = platform_get_drvdata(pdev);

	component_del(&pdev->dev, &atk_dpi_component_ops);
	clk_disable_unprepare(dpi->pclk);

	return 0;
}

static const struct of_device_id atk_dpi_of_match[] = {
	{ .compatible = "atk,atk_dsi" },
	{ }
};
MODULE_DEVICE_TABLE(of, atk_dpi_of_match);

static struct platform_driver atk_dpi_driver = {
	.probe = atk_dpi_probe,
	.remove = atk_dpi_remove,
	.driver = {
		.name = "atk-dsi",
		.of_match_table = atk_dpi_of_match,
	},
};
module_platform_driver(atk_dpi_driver);

EXPORT_SYMBOL(dsi_id);

MODULE_AUTHOR("CX <saurabhs@xilinx.com>");
MODULE_DESCRIPTION("Alientek FPGA MIPI LCD Tx Driver");
MODULE_LICENSE("GPL v2");

