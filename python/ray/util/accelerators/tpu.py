from typing import Optional
from ray._private.accelerators import TPUAcceleratorManager
from ray.util.annotations import PublicAPI


@PublicAPI(stability="alpha")
def get_current_pod_name() -> Optional[str]:
    """
    Return the name of the TPU pod that the worker is a part of.

    Returns:
        The name of the TPU pod. Returns None if not part of a TPU pod.
    """
    tpu_name = TPUAcceleratorManager.get_current_node_tpu_name()
    if tpu_name == "":
        tpu_name = None
    return tpu_name


@PublicAPI(stability="alpha")
def get_current_pod_worker_count() -> Optional[int]:
    """
    Count the number of workers associated with the TPU pod that the worker belongs to.

    Returns:
        The total number of workers in the TPU pod. Returns None if the worker is not
        part of a TPU pod.
    """
    return TPUAcceleratorManager.get_num_workers_in_current_tpu_pod()


@PublicAPI(stablity="alpha")
def get_num_tpu_chips_on_node() -> int:
    """
    Return the number of TPU chips on the node.
    Returns:
        The total number of chips on the TPU node. Returns 0 if none are found.
    """
    return TPUAcceleratorManager.get_current_node_num_accelerators()
