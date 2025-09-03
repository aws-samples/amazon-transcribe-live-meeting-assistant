import { createSignedFetcher } from "aws-sigv4-fetch";
import { GraphQLClient } from "graphql-request";
import { deleteInvite, updateInvite } from "./graphql/mutations.js";
import { DeleteInviteInput, Invite, UpdateInviteInput } from "./graphql/types.js";

type ModifiedInvite = Omit<Invite, "users"> & {
    users: string[];
};

export type Speaker = {
    name: string;
    timestamp: number;
};

export class Details {
    private constructor() {}
    private client: GraphQLClient = new GraphQLClient(process.env.GRAPH_API_URL!, {
        fetch: createSignedFetcher({
            service: "appsync",
            region: process.env.AWS_REGION,
        }),
    });

    public invite!: ModifiedInvite;
    private userStrings!: string;

    public scribeName: string = "Scribe";
    public scribeIdentity!: string;

    public waitingTimeout: number = 300000; // 5 minutes
    public meetingTimeout: number = 21600000; // 6 hours

    public start: boolean = false;

    public startCommand: string = "START";
    public pauseCommand: string = "PAUSE";
    public endCommand: string = "END";

    public introMessages!: string[];
    public startMessages: string[] = [
        "Saving new speakers, messages, and machine-generated captions.",
        `Send "${this.pauseCommand}" in the chat to stop saving meeting details.`,
    ];
    public pauseMessages: string[] = [
        "Not saving speakers, messages, or machine-generated captions.",
        `Send "${this.startCommand}" in the chat to start saving meeting details.`,
    ];

    public messages: string[] = [];
    public attachments: Record<string, string> = {};
    public captions: string[] = [];
    public speakers: Speaker[] = [];

    static async initialize(): Promise<Details> {
        const details = new Details();
        await details.updateInvite("Joining");
        details.updateDetails();
        return details;
    }

    public async updateInvite(status: string) {
        const response: { updateInvite: ModifiedInvite } = await this.client.request(updateInvite, {
            input: {
                id: process.env.INVITE_ID!,
                status: status,
            } as UpdateInviteInput,
        });
        if (!this.invite) {
            this.invite = response.updateInvite;
        }
    }

    private updateDetails() {
        const users = this.invite.users;
        if (users.length === 1) {
            this.userStrings = users[0];
        } else if (users.length === 2) {
            this.userStrings = `${users[0]} and ${users[1]}`;
        } else if (users.length > 2) {
            this.userStrings = `${users.slice(0, -1).join(", ")}, and ${users.slice(-1)}`;
        }

        this.scribeIdentity = `${this.scribeName} [${users[0]}]`;

        this.introMessages = [
            `Hello! I am an AI-assisted scribe. I was invited by ${this.userStrings}.`,
            `If all other participants consent to my use, send "${this.startCommand}" in the chat ` +
                `to start saving new speakers, messages, and machine-generated captions.`,
            `If you do not consent to my use, send "${this.endCommand}" in the chat ` +
                `to remove me from this meeting.`,
        ];
    }

    public async deleteInvite() {
        await this.client.request(deleteInvite, {
            input: {
                id: process.env.INVITE_ID!,
            } as DeleteInviteInput,
        });
    }
}

export const details = await Details.initialize();
