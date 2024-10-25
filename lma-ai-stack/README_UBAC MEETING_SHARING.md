# User Based Access Control: Meeting Sharing Feature
Starting version 0.2.5, LMA users can share meetings that they own with other users. On the Meeting List page of the LMA web UI, you will now be able to see the meetings that you own as well as meetings that have been shared with you. 

In the following illustration, the first meeting was shared with the current user (kkdaws@amazon.com or User 1) by another user (kkdaws+1@amazon.com or User 2). The owner (*Owner Name* or *Owner Email* column) of the meeting has also shared the meeting with other recipient (rstrahan@amazon or User 3) as you can see it under the *Shared With* column. The rest of the meetings are owned by the current user. The second and third meetings were shared with another user (kkdaws+1@amazon.com).

![Share Meeting View User 1](meeting-sharing-view-user-1.png)

The following illustration shows the meetings owned by and shared with User 2. 

![Share Meeting View User 2](meeting-sharing-view-user-2.png)

Note that only the owners of a meeting will be able to share it with others. That is, User 2 will not be able to share a meeting owned by User 1 even though they have access to view the meeting in the UI. In other words, a recipient of a meeting only has *read-only* access to the meeting shared with them by others.

## Sharing one or more meetings with others
To share a meeting, choose one or more meetings that you own that you want to share with others and then choose the *share* icon on the meeting controls located on the upper right corner of the UI.

![Choose one or more meetings](sharing-a-meeting-1.png)

In the *Share Meeting* pop-up, provide a comma-separated list of email addresses of the recipients and choose Submit. Wait for confirmation that the meetings have been successfully shared before closing the pop-up. If you need to share it with additional recipients, you can do immediately after sharing the first set of recipients or do so at a later time. LMA will preserve the original list of recipients and incrementally adds permissions to the new set during subsequent sharing of the same meeting.

## Developer testing / troubleshooting notes

- To run LMA UI locally, see [How to run LMA UI Locally](./source/ui/README.md)
- To contribute to the solution, report bugs or issues, see [Contributing Guidelines](../CONTRIBUTING.md)