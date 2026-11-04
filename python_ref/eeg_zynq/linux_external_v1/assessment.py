"""Limited passive-short assessment; no filtering, repair or waveform claims."""
import math
from . import protocol as w


def assess(raw, plan, powered_fact, *, fixture=False):
    if not plan or not powered_fact:
        return dict(result='UNKNOWN_NO_BOUND_PLAN_OR_CURRENT_MEASUREMENT', pass_scope=None)
    if plan['mode']!='KNOWN_PASSIVE_SHORT':
        return dict(result='WAVEFORM_RESPONSE_NOT_EVALUATED', pass_scope=None,
            limitation='TRANSPORT_AND_CONFIG_VALIDATION_DO_NOT_PROVE_GAIN_OR_FREQUENCY_RESPONSE')
    if len(raw)!=w.RAW_BYTES:
        raise ValueError('STATIC_ASSESSMENT_REQUIRES_ALL256_FRESH_FRAMES')
    measurement=powered_fact['external_measurement']
    values=list(w.EMD1.iter_unpack(raw))
    for sequence,record in enumerate(values):
        if record[:4]!=(0x31444d45,0x00600001,sequence,sequence) or record[22:]!=(4096000,2):
            raise ValueError('STATIC_SOURCE_SEQUENCE_OR_EXTERNAL_IDENTITY')
    metrics=[]
    for channel in plan['stimulated_channels']:
        samples=[v[5+channel] for v in values]
        rms=math.sqrt(sum(sample**2 for sample in samples)/256)
        clipped=any(sample in (-(1<<23),(1<<23)-1) for sample in samples)
        row=dict(logical_channel=channel, minimum_counts=min(samples), maximum_counts=max(samples),
            mean_counts=sum(samples)/256, rms_from_zero_counts=rms,
            measured_fixture_dc_offset_v=measurement['differential_offset_v'], clipped=clipped,
            limit_rms_counts=plan['zero_rms_max_counts'])
        metrics.append(row)
    passed=all(not row['clipped'] and row['rms_from_zero_counts']<=row['limit_rms_counts'] for row in metrics)
    result=dict(result=('LOCAL_FIXTURE_STATIC_CHECK_PASS' if fixture else 'LIMITED_EXTERNAL_PASSIVE_STATIC_CHECK_PASS') if passed else 'EXTERNAL_PASSIVE_STATIC_CHECK_FAIL',
        pass_scope='MEASURED_PASSIVE_DIFFERENTIAL_STATIC_INPUT_TO_LINUX_QT_ONLY' if passed else None,
        fixture_only=fixture, frames=256, metrics=metrics,
        unqualified_channels=[c for c in range(1,17) if c not in plan['stimulated_channels']],
        waveform_gain_or_frequency_response_proven=False, human_or_cap=False,
        reference_voltage_measured=False, voltage_calibration_proven=False,
        processing='ALL_RAW_SAMPLES_NO_SMOOTHING_NO_INTERPOLATION_NO_FILL')
    if not passed:
        raise ValueError(str(result))
    return result
