"""Explicit file-only modes; no acquisition, network or replay-to-target path."""
import argparse
import json

from .model import EVENT28_RAW, RawObserver


def main():
    parser = argparse.ArgumentParser(description="CONT1只读诊断；默认事件28局部回放，不控制硬件")
    parser.add_argument("--raw", default=str(EVENT28_RAW), help="项目build内的raw_dma.bin")
    parser.add_argument("--observe", action="store_true", help="观察已有主控的新落盘文件，不连接设备/网络")
    parser.add_argument("--end-seconds", type=float, default=173.108, help="回放窗口结束的设计秒")
    parser.add_argument("--check", action="store_true", help="仅检查依赖和一个原码窗口，不打开界面")
    args = parser.parse_args()
    from .qt import run
    observer = RawObserver(args.raw, observe=args.observe)
    if args.check:
        import PySide6
        state = observer.read_window(end_seconds=args.end_seconds)
        state.pop("samples")
        print(json.dumps(dict(pyside=PySide6.__version__, hardware_operations=0,
                              scope="ONE_VISIBLE_WINDOW_ONLY", snapshot=state), ensure_ascii=False))
        return 0
    return run(observer, end_seconds=args.end_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
