import puppeteer from "puppeteer";
import Chime from "./chime.js";
import { details } from "./details.js";
import { encapsulate } from "./process.js";
import { transcriptionService } from "./scribe.js";
import Webex from "./webex.js";
import Zoom from "./zoom.js";

const main = async () => {
    const currentTimestamp = Math.floor(Date.now() / 1000);
    const timestampDiff = Math.max(0, (details.invite.meetingTime - currentTimestamp - 10) * 1000);
    console.log(`Sleeping ${timestampDiff / 1000} seconds.`);
    await new Promise((resolve) => setTimeout(resolve, timestampDiff));

    transcriptionService.startTranscription();

    const browser = await puppeteer.launch({
        // headless: false,
        ignoreDefaultArgs: ["--mute-audio"],
        args: [
            "--window-size=2560,1440",
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            "--disable-notifications",
            "--disable-extensions",
            "--disable-crash-reporter",
            "--disable-dev-shm-usage",
            "--no-sandbox",
        ],
    });
    const page = await browser.newPage();
    await page.setViewport(null);
    page.setDefaultTimeout(20000);

    let meeting;
    try {
        if (details.invite.meetingPlatform === "Chime") {
            meeting = new Chime();
        } else if (details.invite.meetingPlatform === "Webex") {
            meeting = new Webex();
        } else if (details.invite.meetingPlatform === "Zoom") {
            meeting = new Zoom();
        } else {
            throw new Error("Meeting platform is unsupported.");
        }
        await meeting.initialize(page);

        await encapsulate();
        await details.updateInvite("Completed");
    } catch (error) {
        console.log("Scribe failed:", error);
        await details.updateInvite("Failed");
    } finally {
        await browser.close();
        transcriptionService.stopTranscription();
        await details.deleteInvite();
    }
};

main();
