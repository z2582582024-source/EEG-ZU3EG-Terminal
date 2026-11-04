"""Frozen electrode and connector metadata for the 16 logical channels.

This module is the software representation of the user-confirmed mapping in
``Hardware/16通道脑电帽推荐映射.md`` and
``Hardware/脑电极引脚与实际ADS1299引脚对应.xlsx``.  Cap lead labels,
ADS1299 inputs, and software logical channels are deliberately separate.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from types import MappingProxyType


@dataclass(frozen=True, slots=True)
class ElectrodeChannel:
    """One frozen positive-input signal-channel connection."""

    logical_index: int
    adc_index: int
    adc_input_index: int
    electrode: str
    cap_lead_index: int
    board_input: str
    functional_group: str

    @property
    def logical_channel(self) -> str:
        return f"CH{self.logical_index:02d}"

    @property
    def adc_input(self) -> str:
        return f"ADS1299-{self.adc_index} IN{self.adc_input_index}P"

    @property
    def cap_lead(self) -> str:
        return f"CH{self.cap_lead_index}"

    def to_dict(self) -> dict[str, int | str]:
        result: dict[str, int | str] = asdict(self)
        result.update(
            logical_channel=self.logical_channel,
            adc_input=self.adc_input,
            cap_lead=self.cap_lead,
        )
        return result


@dataclass(frozen=True, slots=True)
class AuxiliaryElectrode:
    """Reference or BIAS connection that is not a sampled logical channel."""

    role: str
    electrode: str
    cap_label: str
    board_input: str
    ads_connection: str
    system_ground_connection: bool
    description: str

    def to_dict(self) -> dict[str, bool | str]:
        return asdict(self)


ELECTRODE_CHANNELS = (
    ElectrodeChannel(1, 1, 1, "FC3", 5, "1P", "motor_imagery"),
    ElectrodeChannel(2, 1, 2, "FC4", 13, "2P", "motor_imagery"),
    ElectrodeChannel(3, 1, 3, "C3", 6, "3P", "motor_imagery"),
    ElectrodeChannel(4, 1, 4, "Cz", 14, "4P", "motor_imagery"),
    ElectrodeChannel(5, 1, 5, "C4", 7, "5P", "motor_imagery"),
    ElectrodeChannel(6, 1, 6, "CP3", 15, "6P", "motor_imagery"),
    ElectrodeChannel(7, 1, 7, "CPz", 8, "7P", "motor_imagery"),
    ElectrodeChannel(8, 1, 8, "CP4", 16, "8P", "motor_imagery"),
    ElectrodeChannel(9, 2, 1, "PO7", 1, "9P", "ssvep"),
    ElectrodeChannel(10, 2, 2, "PO3", 9, "10P", "ssvep"),
    ElectrodeChannel(11, 2, 3, "POz", 2, "11P", "ssvep"),
    ElectrodeChannel(12, 2, 4, "PO4", 10, "12P", "ssvep"),
    ElectrodeChannel(13, 2, 5, "PO8", 3, "13P", "ssvep"),
    ElectrodeChannel(14, 2, 6, "O1", 11, "14P", "ssvep"),
    ElectrodeChannel(15, 2, 7, "Oz", 4, "15P", "ssvep"),
    ElectrodeChannel(16, 2, 8, "O2", 12, "16P", "ssvep"),
)

REFERENCE_ELECTRODE = AuxiliaryElectrode(
    role="common_reference",
    electrode="FCz",
    cap_label="REF",
    board_input="2SRB1/SRB1",
    ads_connection="both ADS1299 SRB1 and all INxN",
    system_ground_connection=False,
    description="Shared reference for the referential montage",
)

BIAS_ELECTRODE = AuxiliaryElectrode(
    role="bias_drive",
    electrode="AFz",
    cap_label="GND",
    board_input="BIAS",
    ads_connection="ADS1299-1 BIAS feedback network",
    system_ground_connection=False,
    description="Subject common-mode feedback; not AGND or Zynq GND",
)


def _validated_lookup(attribute: str) -> MappingProxyType:
    lookup = {getattr(channel, attribute): channel for channel in ELECTRODE_CHANNELS}
    if len(lookup) != len(ELECTRODE_CHANNELS):
        raise AssertionError(f"duplicate frozen electrode mapping key: {attribute}")
    return MappingProxyType(lookup)


if tuple(channel.logical_index for channel in ELECTRODE_CHANNELS) != tuple(range(1, 17)):
    raise AssertionError("frozen logical channels must be ordered CH01..CH16")
if tuple((channel.adc_index, channel.adc_input_index) for channel in ELECTRODE_CHANNELS) != (
    *((1, input_index) for input_index in range(1, 9)),
    *((2, input_index) for input_index in range(1, 9)),
):
    raise AssertionError("frozen ADS1299 order must be ADS1 IN1P..IN8P then ADS2 IN1P..IN8P")

CHANNEL_BY_LOGICAL_NAME = _validated_lookup("logical_channel")
CHANNEL_BY_ELECTRODE = _validated_lookup("electrode")
CHANNEL_BY_CAP_LEAD = _validated_lookup("cap_lead")


def electrode_mapping_document() -> dict[str, object]:
    """Return a JSON-ready, versioned copy of the frozen mapping."""

    return {
        "schema": "eeg-electrode-mapping-v1",
        "status": "frozen-user-confirmed",
        "classification": "project metadata; not a hardware-validation result",
        "ordering": "software CH01..CH16 / ADS1299 raw output order",
        "channels": [channel.to_dict() for channel in ELECTRODE_CHANNELS],
        "reference": REFERENCE_ELECTRODE.to_dict(),
        "bias": BIAS_ELECTRODE.to_dict(),
    }
