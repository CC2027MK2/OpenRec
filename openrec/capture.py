"""采集源：mss（BitBlt，兼容性最好）与 DXcam（DXGI 桌面复制，低开销）。

- ``MssSource`` 走 GDI BitBlt，任何环境都能用，但 2K/4K 下单帧可达上百毫秒。
- ``DxcamSource`` 走 GPU 桌面复制，抓一帧约 1~3 ms，CPU 占用远低于 BitBlt，
  适合游戏与高帧率；它按输出（显示器）工作，本实现只用于主屏。

两者都输出 BGRA 帧，直接喂给 FFmpeg 的 rawvideo 管道。
"""
from __future__ import annotations

import time

import numpy as np

import mss

_DXCAM = None
_DXGI_OUTPUTS: list[dict] | None = None


def dxcam_module():
    """延迟导入 dxcam，避免未安装时影响启动。"""
    global _DXCAM
    if _DXCAM is None:
        import dxcam  # noqa: PLC0415  (延迟导入，便于回退)
        _DXCAM = dxcam
    return _DXCAM


def dxgi_outputs() -> list[dict]:
    """枚举可用的 DXGI 输出（设备号/输出号/尺寸/旋转），结果缓存。"""
    global _DXGI_OUTPUTS
    if _DXGI_OUTPUTS is not None:
        return _DXGI_OUTPUTS
    dx = dxcam_module()
    found: list[dict] = []
    for dev in range(4):
        for out in range(4):
            cam = None
            try:
                cam = dx.create(device_idx=dev, output_idx=out,
                                output_color="BGRA", max_buffer_len=2)
                found.append({
                    "device": dev,
                    "output": out,
                    "width": int(cam.width),
                    "height": int(cam.height),
                    "rotation": int(getattr(cam, "rotation_angle", 0) or 0),
                })
            except Exception:
                break      # 该设备没有更多输出
            finally:
                if cam is not None:
                    try:
                        cam.release()
                    except Exception:
                        pass
    _DXGI_OUTPUTS = found
    return found


def dxgi_output_for(mon) -> tuple[int, int] | None:
    """把 mss 的显示器几何映射到 DXGI 的 (设备号, 输出号)。

    只接受未旋转的输出：旋转屏的 region 语义与桌面坐标不一致，仍走 mss。
    同尺寸输出不止一个时返回 None（退回 mss），避免把副屏录成主屏。
    """
    if not mon or mon.get("virtual"):
        return None
    w, h = int(mon["width"]), int(mon["height"])
    matches = [item for item in dxgi_outputs()
               if item["width"] == w and item["height"] == h
               and item["rotation"] == 0]
    if len(matches) != 1:
        return None
    return matches[0]["device"], matches[0]["output"]


class FrameSource:
    """一帧采集源的统一接口。"""

    name = "?"
    width = 0
    height = 0

    def grab(self, bbox: tuple[int, int, int, int]):
        raise NotImplementedError

    def recover(self):
        """采集对象损坏（显示器休眠/分辨率切换）后重建。"""

    def close(self):
        pass


class MssSource(FrameSource):
    name = "mss"

    def __init__(self):
        self._sct = mss.mss()
        self._region = None

    def grab(self, bbox):
        x, y, w, h = bbox
        self.width, self.height = w, h
        r = self._region
        if r is None or (r["left"], r["top"], r["width"], r["height"]) != (x, y, w, h):
            self._region = {"left": x, "top": y, "width": w, "height": h}
            r = self._region
        # 直接返回 bytes，不给上层多一次整帧拷贝
        return self._sct.grab(r).bgra

    def recover(self):
        try:
            self._sct.close()
        except Exception:
            pass
        self._sct = mss.mss()
        self._region = None

    def close(self):
        try:
            self._sct.close()
        except Exception:
            pass


class DxcamSource(FrameSource):
    """DXGI 桌面复制采集（任意未旋转输出）。线程内按 target_fps 抓帧到环形缓冲。"""

    name = "dxcam"

    def __init__(self, bbox, fps: int, origin=(0, 0),
                 device_idx: int = 0, output_idx: int | None = None):
        self._bbox = tuple(bbox)
        self.width, self.height = self._bbox[2], self._bbox[3]
        self._fps = max(1, int(fps))
        self._origin = origin
        self._device = device_idx
        self._output = output_idx
        self._ready = False
        self._buf = np.empty((self.height, self.width, 4), dtype=np.uint8)
        self._dx = dxcam_module()
        self._cam = None
        self._start()

    def _start(self):
        try:
            self._cam = self._dx.create(
                device_idx=self._device, output_idx=self._output,
                output_color="BGRA", max_buffer_len=2)
            x, y, w, h = self._bbox
            ox, oy = self._origin
            self._cam.start(
                region=(x - ox, y - oy, x - ox + w, y - oy + h),
                target_fps=self._fps,
                video_mode=True,   # 画面静止时复用上一帧，保证时间轴连续
            )
        except Exception:
            self.close()
            raise
        self._ready = False

    def wait_ready(self, timeout: float = 1.5) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self._cam.grab(copy=False)
            if frame is not None:
                if frame.shape != self._buf.shape:
                    self._buf = np.empty(frame.shape, dtype=np.uint8)
                np.copyto(self._buf, frame)   # 首帧必须落到自有缓冲，避免未初始化内存
                self._ready = True
                return True
            time.sleep(0.01)
        return False

    def grab(self, bbox=None):
        frame = self._cam.grab(copy=False)
        if frame is None:
            # 首帧之前返回 None；之后复用上一帧，避免时间轴断档。
            # 返回副本：调用方会原地叠加光标/水印，复用同一缓冲会累积叠加。
            return self._buf.copy() if self._ready else None
        if frame.shape != self._buf.shape:
            self._buf = np.empty(frame.shape, dtype=np.uint8)
        # 拷进自有缓冲：环形缓冲会被采集线程覆盖，直接引用会撕裂画面
        np.copyto(self._buf, frame)
        self._ready = True
        return self._buf

    def recover(self):
        self.close()
        time.sleep(0.2)
        self._start()

    def close(self):
        cam = self._cam
        self._cam = None
        if cam is None:
            return
        try:
            cam.stop()
        except Exception:
            pass
        try:
            cam.release()
        except Exception:
            pass
