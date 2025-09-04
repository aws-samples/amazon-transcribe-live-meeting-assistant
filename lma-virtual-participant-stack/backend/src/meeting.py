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

            await meeting(page)
            await browser.close()
        
        # Meeting completed successfully
        if status_manager:
            status_manager.set_completed()
            print(f"VP {vp_id} status set to COMPLETED")
            
    except Exception as e:
        print(f"Meeting failed with error: {e}")
        if status_manager:
            status_manager.set_failed(str(e))
            print(f"VP {vp_id} status set to FAILED")
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
