#
# 
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved. 
# SPDX-License-Identifier: MIT-0
# 
#
from details import meeting_platform, meeting_name_with_timestamp, should_record_call
import asyncio
from playwright.async_api import async_playwright
import sys
import os
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


async def app():
    # Initialize status manager if VP_ID is provided
    status_manager = None
    vp_id = os.environ.get('VIRTUAL_PARTICIPANT_ID')
    if vp_id:
        try:
            status_manager = VirtualParticipantStatusManager(vp_id)
            status_manager.set_joining()
        except Exception as e:
            print(f"Failed to initialize status manager: {e}")
    
    try:
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
