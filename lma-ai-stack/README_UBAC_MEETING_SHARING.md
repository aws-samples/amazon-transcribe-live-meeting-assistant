# User Based Access Control: Meeting Sharing Feature
Starting version 0.2.5, LMA users can share meetings that they own with other users. On the Meeting List page of the LMA web UI, you will now be able to see the meetings that you own as well as meetings that have been shared with you. 

In the following illustration, the first meeting was shared with the current user (kkdaws@amazon.com or User 1) by another user (kkdaws+1@amazon.com or User 2). The owner (*Owner Name* or *Owner Email* column) of the meeting has also shared the meeting with other recipient (rstrahan@amazon or User 3) as you can see it under the *Shared With* column. The rest of the meetings are owned by the current user. The second and third meetings were shared with another user (kkdaws+1@amazon.com).

![Share Meeting View User 1](./images/meeting-sharing-view-user-1.png)

The following illustration shows the meetings owned by and shared with User 2. 

![Share Meeting View User 2](./images/meeting-sharing-view-user-2.png)

## Sharing one or more meetings with others
To share a meeting, choose one or more meetings that you own that you want to share with others and then choose the *share* icon on the meeting controls located on the upper right corner of the UI.

![Choose one or more meetings](./images/sharing-a-meeting-1.png)

In the *Share Meeting* pop-up, provide a comma-separated list of email addresses of the recipients and choose Submit. Wait for confirmation that the meetings have been successfully shared before closing the pop-up. If you need to share it with additional recipients, you can do immediately after sharing the first set of recipients or do so at a later time. LMA will preserve the original list of recipients and incrementally adds permissions to the new set during subsequent sharing of the same meeting.

## Features
- Users are now able to share both previous and *live meeting* with other recipients. When sharing a live meeting, there is a possibility that some transcripts might be missing. See [Limitations](#limitations) for additional details.
- Meeting controls to allow only the owner to share the meeting with recipients. That is, User 2 will not be able to share a meeting owned by User 1 even though they have access to view the meeting in the UI. In other words, a recipient of a meeting only has *read-only* access to the meeting shared with them by others.
- Ability to share a meeting with a new user (i.e a user that hasn't signed up for an LMA application). Restrictions apply. See [Limitations](#limitations) for additional details.

## Limitations
- When users share a live meeting with other recipients, there is a small possibility that some of the transcript segments may not be shared with the recipients (due to race condition). This doesn't impact the ability of the recipient to see other meeting details. This limitation will be addressed in a future release.
- Users will be unable to edit their share settings. This means that once you share a meeting with a recipient, you will not be able to remove them. However, you will be able to add additional recipients. A future release will address this limitation.
- Recipients will not get (email) notifications of meetings being shared with them. Existing users that are recipients of a new meeting will be able to see it in the LMA web UI. For new users, the owner of the meeting should share the LMA web CloudFront URL and request them to sign up for a new user in order to be able to see their meeting.
- The sharing functionality currently doesn't validate if the recipient is part of the *Authorized Account Email Domain* that the admin configured during LMA deployment. Even if the meeting share is successful, recipients will not be able to create an account if their email is not part of the *Authorized Account Email Domain*.

## Developer testing / troubleshooting notes

- To run LMA UI locally, see [How to run LMA UI Locally](./source/ui/README.md)
- To contribute to the solution, report bugs or issues, see [Contributing Guidelines](../CONTRIBUTING.md)