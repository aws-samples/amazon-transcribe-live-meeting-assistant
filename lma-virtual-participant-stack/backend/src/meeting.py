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

if meeting_platform == "Chime":
    from chime import meeting
elif meeting_platform == "Zoom":
    from zoom import meeting


async def app():
    # Initialize status manager if VP_ID is provided
    status_manager = None
    vp_id = os.environ.get('VIRTUAL_PARTICIPANT_ID')
    if vp_id:
        try:
            status_manager = VirtualParticipantStatusManager(vp_id)
            status_manager.set_joining()
            print(f"VP {vp_id} status set to JOINING")
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
                # Pass status manager to meeting function so it can update status at right time
                if meeting_platform == "Zoom":
                    await meeting(page, status_manager, vp_id)
                else:
                    await meeting(page)
                
                # If we get here without exception, meeting completed successfully
                # Note: The meeting() function runs until the meeting ends
                
            except Exception as meeting_error:
                # Meeting join failed - check for specific error types
                error_msg = str(meeting_error).lower()
                
                if any(keyword in error_msg for keyword in ['password', 'passcode', 'authentication']):
                    if status_manager:
                        status_manager.set_failed("Wrong meeting password")
                        print(f"VP {vp_id} status set to FAILED - Wrong password")
                elif any(keyword in error_msg for keyword in ['meeting not found', 'invalid meeting', 'meeting id']):
                    if status_manager:
                        status_manager.set_failed("Invalid meeting ID")
                        print(f"VP {vp_id} status set to FAILED - Invalid meeting ID")
                elif any(keyword in error_msg for keyword in ['meeting ended', 'meeting has ended']):
                    if status_manager:
                        status_manager.set_failed("Meeting already ended")
                        print(f"VP {vp_id} status set to FAILED - Meeting ended")
                elif any(keyword in error_msg for keyword in ['permission denied', 'not authorized']):
                    if status_manager:
                        status_manager.set_failed("Permission denied")
                        print(f"VP {vp_id} status set to FAILED - Permission denied")
                else:
                    if status_manager:
                        status_manager.set_failed(f"Meeting join failed: {meeting_error}")
                        print(f"VP {vp_id} status set to FAILED - {meeting_error}")
                raise
            
            await browser.close()
        
        # Meeting completed successfully
        if status_manager:
            status_manager.set_completed()
            print(f"VP {vp_id} status set to COMPLETED")
            
    except Exception as e:
        print(f"Overall error: {e}")
        # Only set failed if we haven't already set a more specific error
        if status_manager and str(e) not in ['Meeting join failed', 'Wrong password', 'Invalid meeting ID']:
            status_manager.set_failed(str(e))
            print(f"VP {vp_id} status set to FAILED - {e}")
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
