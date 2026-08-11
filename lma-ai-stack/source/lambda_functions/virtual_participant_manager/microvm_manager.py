# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

"""
MicroVM Manager for Virtual Participants
Handles termination of Lambda MicroVMs using the id stored in the task registry.

Mirrors ecs_manager.ECSTaskManager for the MICROVM launch type. Without it,
"stop meeting" in the UI had no effect on a MicroVM-hosted VP: the manager only
knew how to call ecs:StopTask, and the registry row for a MicroVM has no
taskArn, so termination silently no-opped.

A MicroVM that is never terminated runs until maximumDurationInSeconds (8 hours,
the service ceiling). One was observed alive for 93 minutes after a failed Zoom
join, transcribing silence into a meeting nobody was in.
"""

import logging
import os
import time

from microvm_client import MicrovmClient, MicrovmError

logger = logging.getLogger(__name__)

# TerminateMicrovm has been observed returning a transient 502 Bad Gateway (an
# HTML error page, not a modelled error) while GetMicrovm on the same URI
# succeeded. Retrying immediately worked, so a few short attempts turn a leaked
# 8-hour MicroVM into a blip.
_TERMINATE_ATTEMPTS = 4
_RETRY_SLEEP_SECONDS = 2


class MicrovmManager:
    """Terminates Lambda MicroVMs for Virtual Participants."""

    def __init__(self):
        # Lazily constructed: this manager is also used on ECS deployments,
        # where no MicroVM call is ever made.
        self._client = None
        logger.info("MicrovmManager initialized")

    @property
    def client(self) -> MicrovmClient:
        if self._client is None:
            self._client = MicrovmClient(
                region=os.environ.get("AWS_REGION", "us-east-1")
            )
        return self._client

    def terminate_microvm(self, microvm_id: str, vp_id: str) -> bool:
        """Terminate the MicroVM running this VP.

        Returns True if the MicroVM is gone (or was already gone), False if it
        may still be running and therefore still billing.
        """
        if not microvm_id:
            logger.error(f"No microvmId recorded for VP {vp_id}")
            return False

        logger.info(f"Terminating MicroVM {microvm_id} for VP {vp_id}")
        for attempt in range(1, _TERMINATE_ATTEMPTS + 1):
            try:
                self.client.terminate_microvm(microvm_id)
                logger.info(f"Successfully terminated MicroVM {microvm_id}")
                return True
            except MicrovmError as exc:
                # Already gone is success, not failure: the VM may have hit its
                # own duration limit or been terminated by a previous attempt.
                if exc.status == 404:
                    logger.info(f"MicroVM {microvm_id} already terminated")
                    return True
                logger.warning(
                    f"TerminateMicrovm attempt {attempt}/{_TERMINATE_ATTEMPTS} "
                    f"for {microvm_id} failed: {exc}"
                )
                if attempt < _TERMINATE_ATTEMPTS:
                    time.sleep(_RETRY_SLEEP_SECONDS)
            except Exception:  # noqa: BLE001 - must not mask the status update
                logger.exception(
                    f"Unexpected error terminating MicroVM {microvm_id}"
                )
                return False

        logger.error(
            f"Could not terminate MicroVM {microvm_id} for VP {vp_id} after "
            f"{_TERMINATE_ATTEMPTS} attempts; it may run until its duration limit"
        )
        return False
