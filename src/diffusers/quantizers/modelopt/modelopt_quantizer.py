from typing import TYPE_CHECKING, Any, Dict, List, Union

from ...utils import (
    get_module_from_name,
    is_accelerate_available,
    is_nvidia_modelopt_available,
    is_nvidia_modelopt_version,
    is_torch_available,
    logging,
)
from ..base import DiffusersQuantizer


if TYPE_CHECKING:
    from ...models.modeling_utils import ModelMixin


if is_torch_available():
    import torch

if is_accelerate_available():
    from accelerate.utils import set_module_tensor_to_device

if is_nvidia_modelopt_available():
    from .utils import _replace_with_modelopt_layers

logger = logging.get_logger(__name__)


class ModelOptQuantizer(DiffusersQuantizer):
    r"""
    Diffusers Quantizer for TensorRT Model Optimizer
    """

    use_keep_in_fp32_modules = True
    requires_calibration = False
    required_packages = ["modelopt"]

    def __init__(self, quantization_config, **kwargs):
        super().__init__(quantization_config, **kwargs)

    def validate_environment(self, *args, **kwargs):
        if not is_nvidia_modelopt_available():
            raise ImportError(
                "Loading an nvidia-modelopt quantized model requires nvidia-modelopt library (`pip install nvidia-modelopt`)"
            )
        if not is_nvidia_modelopt_version(">=", "0.25.0"):
            raise ImportError(
                "Loading an nvidia-modelopt quantized model requires `nvidia-modelopt>=0.25.0`. "
                "Please upgrade your installation with `pip install --upgrade nvidia-modelopt"
            )

        self.offload = False

        device_map = kwargs.get("device_map", None)
        if isinstance(device_map, dict):
            if "cpu" in device_map.values() or "disk" in device_map.values():
                if self.pre_quantized:
                    raise ValueError(
                        "You are attempting to perform cpu/disk offload with a pre-quantized modelopt model "
                        "This is not supported yet. Please remove the CPU or disk device from the `device_map` argument."
                    )
                else:
                    self.offload = True

    def check_if_quantized_param(
        self,
        model: "ModelMixin",
        param_value: "torch.Tensor",
        param_name: str,
        state_dict: Dict[str, Any],
        **kwargs,
    ):
        # ModelOpt imports diffusers internally. This is here to prevent circular imports
        from modelopt.torch.quantization.nn import QuantInputBase, SequentialQuantizer, TensorQuantizer
        from modelopt.torch.quantization.qtensor import BaseQuantizedTensor

        def is_param_quantized(module):
            for _module in module.modules():
                if isinstance(_module, TensorQuantizer) and not _module._dequantize:
                    return True
                elif isinstance(_module, SequentialQuantizer):
                    for q in _module:
                        if isinstance(q, TensorQuantizer) and not q._dequantize:
                            return True
            return False

        module, tensor_name = get_module_from_name(model, param_name)
        if self.pre_quantized and any(isinstance(module, t) for t in [BaseQuantizedTensor]):
            return True
        elif isinstance(module, QuantInputBase) and "weight" in tensor_name:
            return is_param_quantized(module)
        return False

    def create_quantized_param(
        self,
        model: "ModelMixin",
        param_value: "torch.Tensor",
        param_name: str,
        target_device: "torch.device",
        *args,
        **kwargs,
    ):
        """
        Create the quantized parameter by calling .calibrate() after setting it to the module.
        """
        # ModelOpt imports diffusers internally. This is here to prevent circular imports
        import modelopt.torch.quantization as mtq

        dtype = kwargs.get("dtype", torch.float32)
        module, tensor_name = get_module_from_name(model, param_name)
        if self.pre_quantized:
            setattr(module, tensor_name, param_value)
        else:
            config = self.quantization_config.get_config_from_quant_type()
            set_module_tensor_to_device(model, param_name, target_device, param_value, dtype)
            module = mtq.calibrate(module, algorithm=config["algorithm"])
            module.weight.requires_grad = False

    def adjust_max_memory(self, max_memory: Dict[str, Union[int, str]]) -> Dict[str, Union[int, str]]:
        max_memory = {key: val * 0.90 for key, val in max_memory.items()}
        return max_memory

    def adjust_target_dtype(self, target_dtype: "torch.dtype") -> "torch.dtype":
        if self.quantization_config.quant_type == "FP8":
            target_dtype = torch.float8_e4m3fn
        return target_dtype

    def update_torch_dtype(self, torch_dtype: "torch.dtype" = None) -> "torch.dtype":
        if torch_dtype is None:
            logger.info("You did not specify `torch_dtype` in `from_pretrained`. Setting it to `torch.float32`.")
            torch_dtype = torch.float32
        return torch_dtype

    def _process_model_before_weight_loading(
        self,
        model: "ModelMixin",
        device_map,
        keep_in_fp32_modules: List[str] = [],
        **kwargs,
    ):
        self.modules_to_not_convert = self.quantization_config.modules_to_not_convert

        if not isinstance(self.modules_to_not_convert, list):
            self.modules_to_not_convert = [self.modules_to_not_convert]

        self.modules_to_not_convert.extend(keep_in_fp32_modules)

        config = self.quantization_config.get_config_from_quant_type()
        model = _replace_with_modelopt_layers(
            model,
            quantization_config=config,
        )
        model.config.quantization_config = self.quantization_config

    def _process_model_after_weight_loading(self, model, **kwargs):
        return model

    @property
    def is_trainable(self):
        return True

    @property
    def is_serializable(self):
        return True
