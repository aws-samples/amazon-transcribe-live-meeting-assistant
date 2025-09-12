# Copyright (c) 2025 Amazon.com
# This file is licensed under the MIT License.
# See the LICENSE file in the project root for full license information.

#
from details import meeting_platform, meeting_name_with_timestamp, should_record_call
import asyncio
from playwright.async_api import async_playwright
import sys
import os
import signal
import kds
import recording
from status_manager import VirtualParticipantStatusManager

try:
    platform_lower = meeting_platform.lower().strip()
    
    if platform_lower in ["chime", "amazon chime"]:
        from chime import meeting
    elif platform_lower in ["zoom"]:
        from zoom import meeting
    else:
        raise Exception(f"Unsupported meeting platform: '{meeting_platform}'")
except ImportError as e:
    raise Exception(f"Failed to import meeting function for platform '{meeting_platform}': {e}")
except Exception as e:
    raise


# Global variables for graceful shutdown
shutdown_requested = False
status_manager = None
vp_id = None

def store_task_arn_in_registry(vp_id: str):
    """Store task ARN in VPTaskRegistry for efficient termination"""
    try:
        import urllib.request
        import boto3
        import json
        from datetime import datetime, timezone
        
        print(f"Storing task ARN for VP {vp_id} in registry...")
        
        # Get task ARN from ECS metadata endpoint
        metadata_uri = os.environ.get('ECS_CONTAINER_METADATA_URI_V4')
        if not metadata_uri:
            print("ECS_CONTAINER_METADATA_URI_V4 not found - not running in ECS")
            return
        
        # Get task metadata
        task_metadata_url = f"{metadata_uri}/task"
        print(f"Fetching task metadata from: {task_metadata_url}")
        
        with urllib.request.urlopen(task_metadata_url, timeout=10) as response:
            metadata = json.loads(response.read().decode())
            task_arn = metadata.get('TaskARN')
            cluster_arn = metadata.get('Cluster')
            
            print(f"Retrieved task ARN: {task_arn}")
            print(f"Retrieved cluster ARN: {cluster_arn}")
            
            if not task_arn or not cluster_arn:
                print("Could not get task ARN or cluster ARN from metadata")
                return
        
        # Store in VPTaskRegistry table using environment variable
        registry_table_name = os.environ.get('VP_TASK_REGISTRY_TABLE_NAME')
        if not registry_table_name:
            print("VP_TASK_REGISTRY_TABLE_NAME environment variable not set")
            return
        
        print(f"Using VPTaskRegistry table: {registry_table_name}")
        dynamodb = boto3.resource('dynamodb')
        registry_table = dynamodb.Table(registry_table_name)
        
        # Calculate expiry time (24 hours from now)
        expiry_time = int((datetime.now(timezone.utc).timestamp() + 86400))
        
        # Store task details
        registry_table.put_item(
            Item={
                'vpId': vp_id,
                'taskArn': task_arn,
                'clusterArn': cluster_arn,
                'createdAt': datetime.now(timezone.utc).isoformat(),
                'taskStatus': 'RUNNING',
                'expiresAt': expiry_time
            }
        )
        
        print(f"✓ Successfully stored task ARN in registry for VP {vp_id}")
        
    except Exception as e:
        print(f"Error storing task ARN in registry: {e}")
        # This is non-critical - don't fail the container startup

def signal_handler(signum, frame):
    """Handle termination signals gracefully"""
    global shutdown_requested, status_manager, vp_id
    print(f"Received signal {signum}, initiating graceful shutdown...")
    shutdown_requested = True
    
    # Send END event to Kinesis when externally terminated
    try:
        print("Sending END meeting event due to external termination...")
        kds.send_end_meeting()
        print("END meeting event sent successfully")
    except Exception as e:
        print(f"Failed to send END meeting event: {e}")
    
    # Update status to ENDED when externally terminated
    if status_manager and vp_id:
        try:
            status_manager.set_completed()  # Use COMPLETED instead of ENDED for external termination
            print(f"VP {vp_id} status updated to COMPLETED due to external termination")
        except Exception as e:
            print(f"Failed to update status during shutdown: {e}")
    
    # Exit gracefully
    print("Graceful shutdown complete. Exiting...")
    sys.exit(0)

async def app():
    # Initialize status manager if VP_ID is provided
    global status_manager, vp_id
    vp_id = os.environ.get('VIRTUAL_PARTICIPANT_ID')
    if vp_id:
        try:
            status_manager = VirtualParticipantStatusManager(vp_id)
            # Start with INITIALIZING status
            status_manager.set_initializing()
            print(f"VP {vp_id} status: INITIALIZING")
            
            # Store task ARN for efficient termination (separate from join flow)
            try:
                store_task_arn_in_registry(vp_id)
            except Exception as arn_error:
                print(f"Failed to store task ARN (non-critical): {arn_error}")
                
        except Exception as e:
            print(f"Failed to initialize status manager: {e}")
    
    # Set up signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    try:
        # Set CONNECTING status when starting browser
        if status_manager:
            status_manager.set_connecting()
            print(f"VP {vp_id} status: CONNECTING")
        
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                ignore_default_args=["--mute-audio"],
                args=[
                    "--window-size=1920,1080",
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",
                    "--disable-notifications",
                    "--disable-extensions",
                    "--disable-crash-reporter",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            )
            page = await browser.new_page()
            page.set_default_timeout(20000)
            page.on("pageerror", lambda exc: print(f"Uncaught page exception: {exc}"))

            # Set JOINING status before attempting to join meeting
            if status_manager:
                status_manager.set_joining()
                print(f"VP {vp_id} status: JOINING")

            # Try to join meeting - update status based on success/failure
            try:
                await meeting(page, status_manager, vp_id)
                
            except Exception as meeting_error:
                error_msg = str(meeting_error).lower()
                
                if any(keyword in error_msg for keyword in ['password', 'passcode', 'authentication']):
                    if status_manager:
                        status_manager.set_failed("Wrong meeting password")
                elif any(keyword in error_msg for keyword in ['meeting not found', 'invalid meeting', 'meeting id']):
                    if status_manager:
                        status_manager.set_failed("Invalid meeting ID")
                elif any(keyword in error_msg for keyword in ['meeting ended', 'meeting has ended']):
                    if status_manager:
                        status_manager.set_failed("Meeting already ended")
                elif any(keyword in error_msg for keyword in ['permission denied', 'not authorized']):
                    if status_manager:
                        status_manager.set_failed("Permission denied")
                else:
                    if status_manager:
                        status_manager.set_failed(f"Meeting join failed: {meeting_error}")
                raise
            
            await browser.close()
        
        if status_manager:
            status_manager.set_completed()
            
    except Exception as e:
        if status_manager and str(e) not in ['Meeting join failed', 'Wrong password', 'Invalid meeting ID']:
            status_manager.set_failed(str(e))
        raise


print(f"CallId: {meeting_name_with_timestamp}")
asyncio.run(app())
kds.send_end_meeting()
if should_record_call:
    url = recording.upload_recording_to_S3()
    kds.send_call_recording(url)
else:
    print("Call recording not enabled. Skipping recording upload.")
print("Ending Task. Bye.")
sys.exit
