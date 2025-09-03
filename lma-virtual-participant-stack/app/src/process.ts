import { BedrockRuntimeClient, ConverseCommand } from "@aws-sdk/client-bedrock-runtime";
import { ComprehendClient, DetectPiiEntitiesCommand } from "@aws-sdk/client-comprehend";
import * as aws from "@aws-sdk/client-ses";
import * as nodemailer from "nodemailer";
import { details } from "./details.js";

const logsMessage = "Check the CloudWatch logs for more information.";

async function redactPii(text: string, piiExceptions: string[]): Promise<string> {
    if (!text) return text;

    const comprehendClient = new ComprehendClient();
    const command = new DetectPiiEntitiesCommand({
        Text: text,
        LanguageCode: "en",
    });
    const response = await comprehendClient.send(command);

    let resultText = text;
    response.Entities?.forEach((entity) => {
        const entityType = entity.Type;
        if (entityType && !piiExceptions.includes(entityType) && (entity.Score || 0) >= 0.999) {
            const pii = text.slice(entity.BeginOffset, entity.EndOffset);
            resultText = resultText.replace(pii, `[${entityType}]`);
        }
    });
    return resultText;
}

async function summarize(transcript: string): Promise<string> {
    const systemPrompt =
        "You are an AI assistant tasked with outputting meeting notes from a transcript. " +
        "Your notes should capture all relevant details from the meeting " +
        "in a concise and clear manner.";
    const prompt =
        "You will be outputting meeting notes from this transcript:\n" +
        `<transcript>${transcript}</transcript>\n\n` +
        "For each unique topic of discussion, you should output the following items:\n" +
        "1. A title for the discussion topic\n" +
        "2. A list of speakers who participated in the topic's discussion\n" +
        "3. A comprehensive summary of the topic's discussion\n" +
        "4. A list of next steps or action items from the topic's discussion\n\n" +
        "You may omit an item if there is not enough information for it.\n\n" +
        "Format your output in HTML, using the following guidelines:\n" +
        "- Use <html> tags.\n" +
        "- Use <section> tags for topics.\n" +
        "- Use <h3> tags for topic titles.\n" +
        "- Use <h4> tags for item headings (Speakers, Summary, Next Steps).\n" +
        "- Use <p> tags for summaries.\n" +
        "- Use <ul> and <li> tags for lists.";
    const errorMessage = "Error while outputting meeting notes";

    try {
        const bedrockClient = new BedrockRuntimeClient();
        const response = await bedrockClient.send(
            new ConverseCommand({
                modelId: process.env.MODEL_ID!,
                system: [{ text: systemPrompt }],
                messages: [{ role: "user", content: [{ text: prompt }] }],
                inferenceConfig: {
                    maxTokens: 4096,
                    temperature: 0.9,
                    topP: 0.2,
                },
            })
        );
        const responseText = response.output?.message?.content?.[0]?.text ?? "";
        const html = responseText.match(/<html>(.*?)<\/html>/s)?.[1] ?? "";
        // console.log(html)
        return html;
    } catch (error) {
        console.log(`${errorMessage}:`, error);
        return `${errorMessage}. ${logsMessage}`;
    }
}

async function sendEmail(
    chat: string | null,
    attachments: Record<string, string>,
    transcript: string | null
): Promise<void> {
    const ses = new aws.SES();
    const transport = nodemailer.createTransport({
        SES: { ses, aws },
    });

    let html: string;
    if (transcript) {
        html = await summarize(transcript);
    } else {
        html = `Your transcript was empty. ${logsMessage}`;
    }

    const mailOptions: nodemailer.SendMailOptions = {
        from: process.env.EMAIL_SOURCE!,
        to: details.invite.users.join(", "),
        subject: `${details.invite.name} Follow-up`,
        html: html,
        attachments: [],
    };

    if (transcript) {
        mailOptions.attachments?.push({
            filename: "transcript.txt",
            content: transcript,
        });
    }
    if (chat) {
        mailOptions.attachments?.push({
            filename: "chat.txt",
            content: chat,
        });
    }
    for (const [fileName, link] of Object.entries(attachments)) {
        const response = await fetch(link);
        const buffer = await response.arrayBuffer();
        mailOptions.attachments?.push({
            filename: fileName,
            content: Buffer.from(buffer),
        });
    }

    try {
        await transport.sendMail(mailOptions);
        console.log(`Email sent to ${details.invite.users.length} user(s)!`);
    } catch (error) {
        console.log("Error while sending email:", error);
    }
}

export async function encapsulate(): Promise<void> {
    // console.log("Messages:", details.messages);
    // console.log("Attachments:", details.attachments);
    // console.log("Captions:", details.captions);
    // console.log("Speakers:", details.speakers);

    const piiExceptions: string[] = [
        "EMAIL",
        "ADDRESS",
        "NAME",
        "PHONE",
        "DATE_TIME",
        "URL",
        "AGE",
        "USERNAME",
    ];

    const chat = await redactPii(details.messages.join("\n"), piiExceptions);
    const transcript = await redactPii(details.captions.join("\n\n"), piiExceptions);

    // console.log("Chat:", chat);
    // console.log("Transcript:", transcript);

    await sendEmail(chat, details.attachments, transcript);
}
