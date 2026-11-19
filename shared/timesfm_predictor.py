"""
TimesFM 共享预测服务 - 轻量级时间序列预测模块

基于 Google Research TimesFM 2.5 (200M params, 16K context, ICML 2024)
模型: google/timesfm-2.5-200m-pytorch (Apache 2.0)

用法:
    from shared.timesfm_predictor import TimesFMPredictor

    predictor = TimesFMPredictor()
    predictions = predictor.predict([1.0, 2.0, 3.0, ...], horizon=12)
    # predictions: (point_forecast, quantile_forecast)
"""

import logging
import os
from typing import List, Optional, Tuple, Union

import numpy as np

logger = logging.getLogger(__name__)

# 默认模型配置
DEFAULT_MODEL = "google/timesfm-2.5-200m-pytorch"
DEFAULT_CONTEXT = 1024  # 最大上下文长度
DEFAULT_HORIZON = 256   # 最大预测长度

# HuggingFace 镜像（国内用户加速下载）
HF_MIRRORS = [
    "https://hf-mirror.com",
]


def _setup_hf_mirror():
    """自动配置 HuggingFace 镜像（如已设置则跳过）"""
    if os.environ.get("HF_ENDPOINT"):
        return
    for mirror in HF_MIRRORS:
        os.environ["HF_ENDPOINT"] = mirror
        break


class TimesFMPredictor:
    """TimesFM 预测器单例封装"""

    _instance: Optional["TimesFMPredictor"] = None
    _model: Optional[object] = None
    _config: Optional[object] = None

    def __new__(cls, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        max_context: int = DEFAULT_CONTEXT,
        max_horizon: int = DEFAULT_HORIZON,
        normalize_inputs: bool = True,
        use_quantile: bool = True,
        lazy: bool = True,
    ):
        if self._initialized:
            return
        self._initialized = True

        self.model_name = model_name
        self.max_context = max_context
        self.max_horizon = max_horizon
        self.normalize_inputs = normalize_inputs
        self.use_quantile = use_quantile

        if not lazy:
            self._ensure_model()

    def _ensure_model(self):
        """确保模型已加载"""
        if self._model is not None:
            return

        try:
            from timesfm import ForecastConfig, TimesFM_2p5_200M_torch
        except ImportError:
            raise ImportError(
                "TimesFM 未安装。请运行: pip install timesfm[torch]"
            )

        _setup_hf_mirror()
        logger.info(f"加载 TimesFM 模型: {self.model_name} ...")
        torch = __import__("torch")
        torch.set_float32_matmul_precision("high")

        self._model = TimesFM_2p5_200M_torch.from_pretrained(self.model_name)
        self._config = ForecastConfig(
            max_context=self.max_context,
            max_horizon=self.max_horizon,
            normalize_inputs=self.normalize_inputs,
            use_continuous_quantile_head=self.use_quantile,
            force_flip_invariance=True,
            infer_is_positive=True,
            fix_quantile_crossing=True,
        )
        self._model.compile(self._config)
        logger.info("TimesFM 模型加载完成")

    # ─── 核心预测接口 ───────────────────────────────────────────

    def predict(
        self,
        series: Union[List[float], np.ndarray],
        horizon: int = 12,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        单序列预测

        Args:
            series: 时间序列数据 (list or np.ndarray)
            horizon: 预测步数，默认 12

        Returns:
            (point_forecast, quantile_forecast)
            - point_forecast: shape (horizon,) 点预测
            - quantile_forecast: shape (horizon, 10) 分位数预测
              (mean, p10, p20, ..., p90)
        """
        self._ensure_model()
        arr = np.asarray(series, dtype=np.float32)
        point, quantile = self._model.forecast(
            horizon=horizon, inputs=[arr]
        )
        return point[0], quantile[0]

    def predict_batch(
        self,
        series_list: List[Union[List[float], np.ndarray]],
        horizon: int = 12,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        批量预测

        Args:
            series_list: 多个时间序列
            horizon: 预测步数

        Returns:
            (point_forecast, quantile_forecast)
            - point_forecast: shape (N, horizon)
            - quantile_forecast: shape (N, horizon, 10)
        """
        self._ensure_model()
        inputs = [np.asarray(s, dtype=np.float32) for s in series_list]
        return self._model.forecast(horizon=horizon, inputs=inputs)

    def predict_growth(
        self,
        series: Union[List[float], np.ndarray],
        horizon: int = 12,
    ) -> dict:
        """
        预测增长率

        Returns:
            {
                "trend": "up" | "down" | "flat",
                "growth_rate": float,      # 预测期总增长率
                "confidence": float,        # 0-1 置信度
                "point_forecast": ndarray,
                "quantile_forecast": ndarray,
            }
        """
        point, quantile = self.predict(series, horizon)

        last_val = float(np.asarray(series)[-1])
        final_val = float(point[-1])
        growth_rate = (final_val - last_val) / max(abs(last_val), 1e-8)

        # 用分位数宽度估计置信度（越窄越确定）
        p10 = quantile[:, 0]   # p10
        p90 = quantile[:, -1]  # p90
        mean_interval = np.mean(p90 - p10)
        mean_level = np.mean(np.abs(point)) + 1e-8
        confidence = float(np.clip(1.0 - mean_interval / mean_level / 2, 0, 1))

        trend = "flat"
        if growth_rate > 0.05:
            trend = "up"
        elif growth_rate < -0.05:
            trend = "down"

        return {
            "trend": trend,
            "growth_rate": round(growth_rate, 4),
            "confidence": round(confidence, 4),
            "point_forecast": point,
            "quantile_forecast": quantile,
        }

    # ─── 商业场景接口 ───────────────────────────────────────────

    def predict_market_size(
        self,
        historical: Union[List[float], np.ndarray],
        horizon: int = 12,
    ) -> dict:
        """市场规模预测（用于 GE 矩阵、BCG 矩阵等）"""
        return self.predict_growth(historical, horizon)

    def predict_revenue(
        self,
        historical: Union[List[float], np.ndarray],
        horizon: int = 12,
    ) -> dict:
        """收入预测（用于 SWOT 财务分析、KT DA 等）"""
        return self.predict_growth(historical, horizon)

    def predict_user_growth(
        self,
        historical: Union[List[float], np.ndarray],
        horizon: int = 12,
    ) -> dict:
        """用户增长预测"""
        return self.predict_growth(historical, horizon)

    def predict_seasonal(
        self,
        historical: Union[List[float], np.ndarray],
        horizon: int = 12,
    ) -> np.ndarray:
        """
        季节性预测，仅返回点预测

        Args:
            historical: 历史数据（至少覆盖一个完整周期）
            horizon: 预测步数

        Returns:
            point_forecast: shape (horizon,)
        """
        point, _ = self.predict(historical, horizon)
        return point

    # ─── 工具方法 ───────────────────────────────────────────────

    def is_available(self) -> bool:
        """检查 TimesFM 是否可用（不触发加载）"""
        if self._model is not None:
            return True
        try:
            __import__("timesfm")
            return True
        except ImportError:
            return False

    def unload(self):
        """卸载模型释放内存"""
        self._model = None
        self._config = None
        TimesFMPredictor._model = None
        TimesFMPredictor._config = None
        logger.info("TimesFM 模型已卸载")


# ─── 便捷函数 ───────────────────────────────────────────────────

_predictor: Optional[TimesFMPredictor] = None


def _get_predictor() -> TimesFMPredictor:
    global _predictor
    if _predictor is None:
        _predictor = TimesFMPredictor(lazy=True)
    return _predictor


def quick_predict(
    series: Union[List[float], np.ndarray],
    horizon: int = 12,
) -> Tuple[np.ndarray, np.ndarray]:
    """一键预测（自动加载模型）"""
    return _get_predictor().predict(series, horizon)


def quick_growth(
    series: Union[List[float], np.ndarray],
    horizon: int = 12,
) -> dict:
    """一键增长预测"""
    return _get_predictor().predict_growth(series, horizon)
