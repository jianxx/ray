"""Autoscaler monitoring loop daemon."""

import argparse
import json
import logging
import os
import signal
import sys
import time
import traceback
from collections import Counter
from dataclasses import asdict
from typing import Any, Callable, Dict, Optional, Union

import ray
import ray._private.ray_constants as ray_constants
from ray._common.ray_constants import (
    LOGGING_ROTATE_BYTES,
    LOGGING_ROTATE_BACKUP_COUNT,
)
from ray._private.event.event_logger import get_event_logger
from ray._private.ray_logging import setup_component_logger
from ray._raylet import GcsClient
from ray.autoscaler._private.autoscaler import StandardAutoscaler
from ray.autoscaler._private.commands import teardown_cluster
from ray.autoscaler._private.constants import (
    AUTOSCALER_MAX_RESOURCE_DEMAND_VECTOR_SIZE,
    AUTOSCALER_METRIC_PORT,
    AUTOSCALER_UPDATE_INTERVAL_S,
    DISABLE_LAUNCH_CONFIG_CHECK_KEY,
)
from ray.autoscaler._private.event_summarizer import EventSummarizer
from ray.autoscaler._private.load_metrics import LoadMetrics
from ray.autoscaler._private.prom_metrics import AutoscalerPrometheusMetrics
from ray.autoscaler._private.util import format_readonly_node_type
from ray.autoscaler.v2.sdk import get_cluster_resource_state
from ray.core.generated import gcs_pb2
from ray.core.generated.event_pb2 import Event as RayEvent
from ray.experimental.internal_kv import (
    _initialize_internal_kv,
    _internal_kv_del,
    _internal_kv_get,
    _internal_kv_initialized,
    _internal_kv_put,
)
from ray._private import logging_utils

try:
    import prometheus_client
except ImportError:
    prometheus_client = None


logger = logging.getLogger(__name__)


def parse_resource_demands(resource_load_by_shape):
    """Handle the message.resource_load_by_shape protobuf for the demand
    based autoscaling. Catch and log all exceptions so this doesn't
    interfere with the utilization based autoscaler until we're confident
    this is stable. Worker queue backlogs are added to the appropriate
    resource demand vector.

    Args:
        resource_load_by_shape (pb2.gcs.ResourceLoad): The resource demands
            in protobuf form or None.

    Returns:
        List[ResourceDict]: Waiting bundles (ready and feasible).
        List[ResourceDict]: Infeasible bundles.
    """
    waiting_bundles, infeasible_bundles = [], []
    try:
        for resource_demand_pb in list(resource_load_by_shape.resource_demands):
            request_shape = dict(resource_demand_pb.shape)
            for _ in range(resource_demand_pb.num_ready_requests_queued):
                waiting_bundles.append(request_shape)
            for _ in range(resource_demand_pb.num_infeasible_requests_queued):
                infeasible_bundles.append(request_shape)

            # Infeasible and ready states for tasks are (logically)
            # mutually exclusive.
            if resource_demand_pb.num_infeasible_requests_queued > 0:
                backlog_queue = infeasible_bundles
            else:
                backlog_queue = waiting_bundles
            for _ in range(resource_demand_pb.backlog_size):
                backlog_queue.append(request_shape)
            if (
                len(waiting_bundles + infeasible_bundles)
                > AUTOSCALER_MAX_RESOURCE_DEMAND_VECTOR_SIZE
            ):
                break
    except Exception:
        logger.exception("Failed to parse resource demands.")

    return waiting_bundles, infeasible_bundles


# Readonly provider config (e.g., for laptop mode, manually setup clusters).
BASE_READONLY_CONFIG = {
    "cluster_name": "default",
    "max_workers": 0,
    "upscaling_speed": 1.0,
    "docker": {},
    "idle_timeout_minutes": 0,
    "provider": {
        "type": "readonly",
        "use_node_id_as_ip": True,  # For emulated multi-node on laptop.
        DISABLE_LAUNCH_CONFIG_CHECK_KEY: True,  # No launch check.
    },
    "auth": {},
    "available_node_types": {
        "ray.head.default": {"resources": {}, "node_config": {}, "max_workers": 0}
    },
    "head_node_type": "ray.head.default",
    "file_mounts": {},
    "cluster_synced_files": [],
    "file_mounts_sync_continuously": False,
    "rsync_exclude": [],
    "rsync_filter": [],
    "initialization_commands": [],
    "setup_commands": [],
    "head_setup_commands": [],
    "worker_setup_commands": [],
    "head_start_ray_commands": [],
    "worker_start_ray_commands": [],
}


class Monitor:
    """Autoscaling monitor.

    This process periodically collects stats from the GCS and triggers
    autoscaler updates.
    """

    def __init__(
        self,
        address: str,
        autoscaling_config: Union[str, Callable[[], Dict[str, Any]]],
        log_dir: str = None,
        prefix_cluster_info: bool = False,
        monitor_ip: Optional[str] = None,
        retry_on_failure: bool = True,
    ):
        self.gcs_address = address
        worker = ray._private.worker.global_worker
        # TODO: eventually plumb ClusterID through to here
        self.gcs_client = GcsClient(address=self.gcs_address)

        _initialize_internal_kv(self.gcs_client)

        if monitor_ip:
            monitor_addr = f"{monitor_ip}:{AUTOSCALER_METRIC_PORT}"
            self.gcs_client.internal_kv_put(
                b"AutoscalerMetricsAddress", monitor_addr.encode(), True, None
            )
        self._session_name = self.get_session_name(self.gcs_client)
        logger.info(f"session_name: {self._session_name}")
        worker.mode = 0
        head_node_ip = self.gcs_address.split(":")[0]

        self.load_metrics = LoadMetrics()
        self.last_avail_resources = None
        self.event_summarizer = EventSummarizer()
        self.prefix_cluster_info = prefix_cluster_info
        self.retry_on_failure = retry_on_failure
        self.autoscaling_config = autoscaling_config
        self.autoscaler = None
        # If set, we are in a manually created cluster (non-autoscaling) and
        # simply mirroring what the GCS tells us the cluster node types are.
        self.readonly_config = None

        if log_dir:
            try:
                self.event_logger = get_event_logger(
                    RayEvent.SourceType.AUTOSCALER, log_dir
                )
            except Exception:
                self.event_logger = None
        else:
            self.event_logger = None

        self.prom_metrics = AutoscalerPrometheusMetrics(session_name=self._session_name)

        if monitor_ip and prometheus_client:
            # If monitor_ip wasn't passed in, then don't attempt to start the
            # metric server to keep behavior identical to before metrics were
            # introduced
            try:
                logger.info(
                    "Starting autoscaler metrics server on port {}".format(
                        AUTOSCALER_METRIC_PORT
                    )
                )
                kwargs = {"addr": "127.0.0.1"} if head_node_ip == "127.0.0.1" else {}
                prometheus_client.start_http_server(
                    port=AUTOSCALER_METRIC_PORT,
                    registry=self.prom_metrics.registry,
                    **kwargs,
                )

                # Reset some gauges, since we don't know which labels have
                # leaked if the autoscaler was restarted.
                self.prom_metrics.pending_nodes.clear()
                self.prom_metrics.active_nodes.clear()
            except Exception:
                logger.exception(
                    "An exception occurred while starting the metrics server."
                )
        elif not prometheus_client:
            logger.warning(
                "`prometheus_client` not found, so metrics will not be exported."
            )

        logger.info("Monitor: Started")

    def _initialize_autoscaler(self):
        if self.autoscaling_config:
            autoscaling_config = self.autoscaling_config
        else:
            # This config mirrors the current setup of the manually created
            # cluster. Each node gets its own unique node type.
            self.readonly_config = BASE_READONLY_CONFIG

            # Note that the "available_node_types" of the config can change.
            def get_latest_readonly_config():
                return self.readonly_config

            autoscaling_config = get_latest_readonly_config
        self.autoscaler = StandardAutoscaler(
            autoscaling_config,
            self.load_metrics,
            self.gcs_client,
            self._session_name,
            prefix_cluster_info=self.prefix_cluster_info,
            event_summarizer=self.event_summarizer,
            prom_metrics=self.prom_metrics,
        )

    def update_load_metrics(self):
        """Fetches resource usage data from GCS and updates load metrics."""

        response = self.gcs_client.get_all_resource_usage(timeout=60)
        resources_batch_data = response.resource_usage_data
        log_resource_batch_data_if_desired(resources_batch_data)

        # This is a workaround to get correct idle_duration_ms
        # from "get_cluster_resource_state"
        # ref: https://github.com/ray-project/ray/pull/48519#issuecomment-2481659346
        cluster_resource_state = get_cluster_resource_state(self.gcs_client)
        ray_node_states = cluster_resource_state.node_states
        ray_nodes_idle_duration_ms_by_id = {
            node.node_id: node.idle_duration_ms for node in ray_node_states
        }

        # Tell the readonly node provider what nodes to report.
        if self.readonly_config:
            new_nodes = []
            for msg in list(resources_batch_data.batch):
                node_id = msg.node_id.hex()
                new_nodes.append((node_id, msg.node_manager_address))
            self.autoscaler.provider._set_nodes(new_nodes)

        mirror_node_types = {}
        cluster_full = False
        if (
            hasattr(response, "cluster_full_of_actors_detected_by_gcs")
            and response.cluster_full_of_actors_detected_by_gcs
        ):
            # GCS has detected the cluster full of actors.
            cluster_full = True
        for resource_message in resources_batch_data.batch:
            node_id = resource_message.node_id
            # Generate node type config based on GCS reported node list.
            if self.readonly_config:
                # Keep prefix in sync with ReadonlyNodeProvider.
                node_type = format_readonly_node_type(node_id.hex())
                resources = {}
                for k, v in resource_message.resources_total.items():
                    resources[k] = v
                mirror_node_types[node_type] = {
                    "resources": resources,
                    "node_config": {},
                    "max_workers": 1,
                }
            if (
                hasattr(resource_message, "cluster_full_of_actors_detected")
                and resource_message.cluster_full_of_actors_detected
            ):
                # A worker node has detected the cluster full of actors.
                cluster_full = True
            total_resources = dict(resource_message.resources_total)
            available_resources = dict(resource_message.resources_available)

            waiting_bundles, infeasible_bundles = parse_resource_demands(
                resources_batch_data.resource_load_by_shape
            )

            pending_placement_groups = list(
                resources_batch_data.placement_group_load.placement_group_data
            )

            use_node_id_as_ip = self.autoscaler is not None and self.autoscaler.config[
                "provider"
            ].get("use_node_id_as_ip", False)

            # "use_node_id_as_ip" is a hack meant to address situations in
            # which there's more than one Ray node residing at a given ip.
            # TODO (Dmitri): Stop using ips as node identifiers.
            # https://github.com/ray-project/ray/issues/19086
            if use_node_id_as_ip:
                peloton_id = total_resources.get("NODE_ID_AS_RESOURCE")
                # Legacy support https://github.com/ray-project/ray/pull/17312
                if peloton_id is not None:
                    ip = str(int(peloton_id))
                else:
                    ip = node_id.hex()
            else:
                ip = resource_message.node_manager_address

            idle_duration_s = 0.0
            if node_id in ray_nodes_idle_duration_ms_by_id:
                idle_duration_s = ray_nodes_idle_duration_ms_by_id[node_id] / 1000
            else:
                logger.warning(
                    f"node_id {node_id} not found in ray_nodes_idle_duration_ms_by_id"
                )

            self.load_metrics.update(
                ip,
                node_id,
                total_resources,
                available_resources,
                idle_duration_s,
                waiting_bundles,
                infeasible_bundles,
                pending_placement_groups,
                cluster_full,
            )
        if self.readonly_config:
            self.readonly_config["available_node_types"].update(mirror_node_types)

    def get_session_name(self, gcs_client: GcsClient) -> Optional[str]:
        """Obtain the session name from the GCS.

        If the GCS doesn't respond, session name is considered None.
        In this case, the metrics reported from the monitor won't have
        the correct session name.
        """
        if not _internal_kv_initialized():
            return None

        session_name = gcs_client.internal_kv_get(
            b"session_name",
            ray_constants.KV_NAMESPACE_SESSION,
            timeout=10,
        )

        if session_name:
            session_name = session_name.decode()

        return session_name

    def update_resource_requests(self):
        """Fetches resource requests from the internal KV and updates load."""
        if not _internal_kv_initialized():
            return
        data = _internal_kv_get(
            ray._private.ray_constants.AUTOSCALER_RESOURCE_REQUEST_CHANNEL
        )
        if data:
            try:
                resource_request = json.loads(data)
                self.load_metrics.set_resource_requests(resource_request)
            except Exception:
                logger.exception("Error parsing resource requests")

    def _run(self):
        """Run the monitor loop."""

        while True:
            try:
                gcs_request_start_time = time.time()
                self.update_load_metrics()
                gcs_request_time = time.time() - gcs_request_start_time
                self.update_resource_requests()
                self.update_event_summary()
                load_metrics_summary = self.load_metrics.summary()
                status = {
                    "gcs_request_time": gcs_request_time,
                    "time": time.time(),
                    "monitor_pid": os.getpid(),
                }

                if self.autoscaler and not self.load_metrics:
                    # load_metrics is Falsey iff we haven't collected any
                    # resource messages from the GCS, which can happen at startup if
                    # the GCS hasn't yet received data from the Raylets.
                    # In this case, do not do an autoscaler update.
                    # Wait to get load metrics.
                    logger.info(
                        "Autoscaler has not yet received load metrics. Waiting."
                    )
                elif self.autoscaler:
                    # Process autoscaling actions
                    update_start_time = time.time()
                    self.autoscaler.update()
                    status["autoscaler_update_time"] = time.time() - update_start_time
                    autoscaler_summary = self.autoscaler.summary()
                    try:
                        self.emit_metrics(
                            load_metrics_summary,
                            autoscaler_summary,
                            self.autoscaler.all_node_types,
                        )
                    except Exception:
                        logger.exception("Error emitting metrics")

                    if autoscaler_summary:
                        status["autoscaler_report"] = asdict(autoscaler_summary)
                        status[
                            "non_terminated_nodes_time"
                        ] = (
                            self.autoscaler.non_terminated_nodes.non_terminated_nodes_time  # noqa: E501
                        )

                    for msg in self.event_summarizer.summary():
                        # Need to prefix each line of the message for the lines to
                        # get pushed to the driver logs.
                        for line in msg.split("\n"):
                            logger.info(
                                "{}{}".format(
                                    ray_constants.LOG_PREFIX_EVENT_SUMMARY, line
                                )
                            )
                            if self.event_logger:
                                self.event_logger.info(line)

                    self.event_summarizer.clear()

                status["load_metrics_report"] = asdict(load_metrics_summary)
                as_json = json.dumps(status)
                if _internal_kv_initialized():
                    _internal_kv_put(
                        ray_constants.DEBUG_AUTOSCALING_STATUS, as_json, overwrite=True
                    )
            except Exception:
                # By default, do not exit the monitor on failure.
                if self.retry_on_failure:
                    logger.exception("Monitor: Execution exception. Trying again...")
                else:
                    raise

            # Wait for a autoscaler update interval before processing the next
            # round of messages.
            time.sleep(AUTOSCALER_UPDATE_INTERVAL_S)

    def emit_metrics(self, load_metrics_summary, autoscaler_summary, node_types):
        if autoscaler_summary is None:
            return None

        for resource_name in ["CPU", "GPU", "TPU"]:
            _, total = load_metrics_summary.usage.get(resource_name, (0, 0))
            pending = autoscaler_summary.pending_resources.get(resource_name, 0)
            self.prom_metrics.cluster_resources.labels(
                resource=resource_name,
                SessionName=self.prom_metrics.session_name,
            ).set(total)
            self.prom_metrics.pending_resources.labels(
                resource=resource_name,
                SessionName=self.prom_metrics.session_name,
            ).set(pending)

        pending_node_count = Counter()
        for _, node_type, _ in autoscaler_summary.pending_nodes:
            pending_node_count[node_type] += 1

        for node_type, count in autoscaler_summary.pending_launches.items():
            pending_node_count[node_type] += count

        for node_type in node_types:
            count = pending_node_count[node_type]
            self.prom_metrics.pending_nodes.labels(
                SessionName=self.prom_metrics.session_name,
                NodeType=node_type,
            ).set(count)

        for node_type in node_types:
            count = autoscaler_summary.active_nodes.get(node_type, 0)
            self.prom_metrics.active_nodes.labels(
                SessionName=self.prom_metrics.session_name,
                NodeType=node_type,
            ).set(count)

        failed_node_counts = Counter()
        for _, node_type in autoscaler_summary.failed_nodes:
            failed_node_counts[node_type] += 1

        # NOTE: This metric isn't reset with monitor resets. This means it will
        # only be updated when the autoscaler' node tracker remembers failed
        # nodes. If the node type failure is evicted from the autoscaler, the
        # metric may not update for a while.
        for node_type, count in failed_node_counts.items():
            self.prom_metrics.recently_failed_nodes.labels(
                SessionName=self.prom_metrics.session_name,
                NodeType=node_type,
            ).set(count)

    def update_event_summary(self):
        """Report the current size of the cluster.

        To avoid log spam, only cluster size changes (CPU, GPU or TPU count change)
        are reported to the event summarizer. The event summarizer will report
        only the latest cluster size per batch.
        """
        avail_resources = self.load_metrics.resources_avail_summary()
        if not self.readonly_config and avail_resources != self.last_avail_resources:
            self.event_summarizer.add(
                "Resized to {}.",  # e.g., Resized to 100 CPUs, 4 GPUs, 4 TPUs.
                quantity=avail_resources,
                aggregate=lambda old, new: new,
            )
            self.last_avail_resources = avail_resources

    def destroy_autoscaler_workers(self):
        """Cleanup the autoscaler, in case of an exception in the run() method.

        We kill the worker nodes, but retain the head node in order to keep
        logs around, keeping costs minimal. This monitor process runs on the
        head node anyway, so this is more reliable."""

        if self.autoscaler is None:
            return  # Nothing to clean up.

        if self.autoscaling_config is None:
            # This is a logic error in the program. Can't do anything.
            logger.error("Monitor: Cleanup failed due to lack of autoscaler config.")
            return

        logger.info("Monitor: Exception caught. Taking down workers...")
        clean = False
        while not clean:
            try:
                teardown_cluster(
                    config_file=self.autoscaling_config,
                    yes=True,  # Non-interactive.
                    workers_only=True,  # Retain head node for logs.
                    override_cluster_name=None,
                    keep_min_workers=True,  # Retain minimal amount of workers.
                )
                clean = True
                logger.info("Monitor: Workers taken down.")
            except Exception:
                logger.error("Monitor: Cleanup exception. Trying again...")
                time.sleep(2)

    def _handle_failure(self, error):
        if (
            self.autoscaler is not None
            and os.environ.get("RAY_AUTOSCALER_FATESHARE_WORKERS", "") == "1"
        ):
            self.autoscaler.kill_workers()
            # Take down autoscaler workers if necessary.
            self.destroy_autoscaler_workers()

        # Something went wrong, so push an error to all current and future
        # drivers.
        message = f"The autoscaler failed with the following error:\n{error}"
        if _internal_kv_initialized():
            _internal_kv_put(
                ray_constants.DEBUG_AUTOSCALING_ERROR, message, overwrite=True
            )
        from ray._private.utils import publish_error_to_driver

        publish_error_to_driver(
            ray_constants.MONITOR_DIED_ERROR,
            message,
            gcs_client=self.gcs_client,
        )

    def _signal_handler(self, sig, frame):
        try:
            self._handle_failure(
                f"Terminated with signal {sig}\n"
                + "".join(traceback.format_stack(frame))
            )
        except Exception:
            logger.exception("Monitor: Failure in signal handler.")
        sys.exit(sig + 128)

    def run(self):
        # Register signal handlers for autoscaler termination.
        # Signals will not be received on windows
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        try:
            if _internal_kv_initialized():
                # Delete any previous autoscaling errors.
                _internal_kv_del(ray_constants.DEBUG_AUTOSCALING_ERROR)
            self._initialize_autoscaler()
            self._run()
        except Exception:
            logger.exception("Error in monitor loop")
            self._handle_failure(traceback.format_exc())
            raise


def log_resource_batch_data_if_desired(
    resources_batch_data: gcs_pb2.ResourceUsageBatchData,
) -> None:
    if os.getenv("AUTOSCALER_LOG_RESOURCE_BATCH_DATA") == "1":
        logger.info("Logging raw resource message pulled from GCS.")
        logger.info(resources_batch_data)
        logger.info("Done logging raw resource message.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=("Parse GCS server for the monitor to connect to.")
    )
    parser.add_argument(
        "--gcs-address", required=False, type=str, help="The address (ip:port) of GCS."
    )
    parser.add_argument(
        "--autoscaling-config",
        required=False,
        type=str,
        help="the path to the autoscaling config file",
    )
    parser.add_argument(
        "--logging-level",
        required=False,
        type=str,
        default=ray_constants.LOGGER_LEVEL,
        choices=ray_constants.LOGGER_LEVEL_CHOICES,
        help=ray_constants.LOGGER_LEVEL_HELP,
    )
    parser.add_argument(
        "--logging-format",
        required=False,
        type=str,
        default=ray_constants.LOGGER_FORMAT,
        help=ray_constants.LOGGER_FORMAT_HELP,
    )
    parser.add_argument(
        "--logging-filename",
        required=False,
        type=str,
        default=ray_constants.MONITOR_LOG_FILE_NAME,
        help="Specify the name of log file, "
        "log to stdout if set empty, default is "
        f'"{ray_constants.MONITOR_LOG_FILE_NAME}"',
    )
    parser.add_argument(
        "--logs-dir",
        required=True,
        type=str,
        help="Specify the path of the temporary directory used by Ray processes.",
    )
    parser.add_argument(
        "--logging-rotate-bytes",
        required=False,
        type=int,
        default=LOGGING_ROTATE_BYTES,
        help="Specify the max bytes for rotating "
        "log file, default is "
        f"{LOGGING_ROTATE_BYTES} bytes.",
    )
    parser.add_argument(
        "--logging-rotate-backup-count",
        required=False,
        type=int,
        default=LOGGING_ROTATE_BACKUP_COUNT,
        help="Specify the backup count of rotated log file, default is "
        f"{LOGGING_ROTATE_BACKUP_COUNT}.",
    )
    parser.add_argument(
        "--monitor-ip",
        required=False,
        type=str,
        default=None,
        help="The IP address of the machine hosting the monitor process.",
    )
    parser.add_argument(
        "--stdout-filepath",
        required=False,
        type=str,
        default="",
        help="The filepath to dump monitor stdout.",
    )
    parser.add_argument(
        "--stderr-filepath",
        required=False,
        type=str,
        default="",
        help="The filepath to dump monitor stderr.",
    )

    args = parser.parse_args()

    # Disable log rotation for windows, because NTFS doesn't allow file deletion when there're multiple owners or borrowers, which happens to be how ray accesses log files.
    logging_rotation_bytes = args.logging_rotate_bytes if sys.platform != "win32" else 0
    logging_rotation_backup_count = (
        args.logging_rotate_backup_count if sys.platform != "win32" else 1
    )
    setup_component_logger(
        logging_level=args.logging_level,
        logging_format=args.logging_format,
        log_dir=args.logs_dir,
        filename=args.logging_filename,
        max_bytes=logging_rotation_bytes,
        backup_count=logging_rotation_backup_count,
    )

    # Setup stdout/stderr redirect files if redirection enabled.
    logging_utils.redirect_stdout_stderr_if_needed(
        args.stdout_filepath,
        args.stderr_filepath,
        logging_rotation_bytes,
        logging_rotation_backup_count,
    )

    logger.info(f"Starting monitor using ray installation: {ray.__file__}")
    logger.info(f"Ray version: {ray.__version__}")
    logger.info(f"Ray commit: {ray.__commit__}")
    logger.info(f"Monitor started with command: {sys.argv}")

    if args.autoscaling_config:
        autoscaling_config = os.path.expanduser(args.autoscaling_config)
    else:
        autoscaling_config = None

    bootstrap_address = args.gcs_address
    if bootstrap_address is None:
        raise ValueError("--gcs-address must be set!")

    monitor = Monitor(
        bootstrap_address,
        autoscaling_config,
        log_dir=args.logs_dir,
        monitor_ip=args.monitor_ip,
    )

    monitor.run()
