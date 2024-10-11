from details import meeting_platform, meeting_name_with_timestamp, should_record_call
import asyncio
from playwright.async_api import async_playwright
import sys
import kds
import recording

if meeting_platform == "Chime":
    from chime import meeting
elif meeting_platform == "Zoom":
    from zoom import meeting


async def app():
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
