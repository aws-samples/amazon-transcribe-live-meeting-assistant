import { API } from 'aws-amplify';
import React, { useState } from 'react';
import { Alert, Button, Modal, Multiselect, SpaceBetween, FormField, Input, Form } from '@awsui/components-react';
import meetingControls from '../../graphql/queries/meetingControls';

export const shareMeetings = async (props, meetingRecipients) => {
  const { calls } = props;
  const getListKeys = (callId, createdAt) => {
    const SHARDS_IN_DAY = 6;
    const SHARD_DIVIDER = 24 / SHARDS_IN_DAY;

    const now = new Date(createdAt);
    const date = now.toISOString().substring(0, 10);
    const hour = now.getUTCHours();

    const hourShard = Math.floor(hour / SHARD_DIVIDER);
    const shardPad = hourShard.toString().padStart(2, '0');

    const listPK = `cls#${date}#s#${shardPad}`;
    const listSK = `ts#${createdAt}#id#${callId}`;

    return { listPK, listSK };
  };

  // Get PK and SK from calls
  const callsWithKeys = props.selectedItems.map(({ callId }) => {
    const call = calls.find((c) => c.CallId === callId);

    let listPK = call.ListPK;
    let listSK = call.ListSK;

    if (!listPK || !listSK) {
      const result = getListKeys(call.CallId, call.CreatedAt);
      listPK = result.listPK;
      listSK = result.listSK;
    }
    return {
      ListPK: listPK,
      ListSK: listSK,
      CallId: call.CallId,
    };
  });

  const response = await API.graphql({
    query: meetingControls,
    variables: {
      input: { Calls: callsWithKeys, MeetingRecipients: meetingRecipients },
    },
  });

  const result = response.data.meetingControls.Result;

  return result;
};

export const shareModal = (props) => {
  const [share, setShare] = useState(false);
  const [meetingRecipients, setMeetingRecipients] = useState([]);
  const [newRecipients, setNewRecipients] = useState('');
  const [submit, setSubmit] = useState(false);
  const [shareResult, setShareResult] = useState(null);

  const { getCallDetailsFromCallIds } = props;

  const openShareSettings = async () => {
    setShare(true);
    const response = await getCallDetailsFromCallIds(props.selectedItems.map((c) => c.callId));
    console.log('CALL SHARE MEETINGS RESPONSE:', response);
  };

  const closeShareSettings = () => {
    setShare(false);
    setMeetingRecipients([]);
    setNewRecipients('');
    setShareResult(null);
  };

  const handleAddRecipients = () => {
    if (newRecipients.trim()) {
      const emailList = newRecipients.split(',').map((email) => email.trim());
      const validEmails = emailList.filter((email) => {
        // Basic email validation regex
        const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
        return emailRegex.test(email) && !meetingRecipients.includes(email);
      });
      setMeetingRecipients([...meetingRecipients, ...validEmails]);
      setNewRecipients('');
    }
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setSubmit(true);
    console.log('Meeting Recipients: ', meetingRecipients);
    const result = await shareMeetings(props, meetingRecipients.join(','));
    setMeetingRecipients([]);
    setSubmit(false);
    setShareResult(result);
  };

  return (
    <SpaceBetween size="xxs" direction="horizontal">
      <Button
        iconName="share"
        variant="normal"
        loading={props.loading}
        disabled={props.selectedItems.length === 0}
        onClick={openShareSettings}
      />
      <Modal
        onDismiss={closeShareSettings}
        visible={share}
        footer={
          <Form
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button formAction="none" onClick={closeShareSettings}>
                  Close
                </Button>
                <Button variant="primary" disabled={submit || meetingRecipients.length === 0} onClick={handleSubmit}>
                  Submit
                </Button>
              </SpaceBetween>
            }
          >
            <Alert type="info" visible={shareResult}>
              {shareResult}
            </Alert>
          </Form>
        }
        header={<h3>Share Meeting</h3>}
      >
        <SpaceBetween size="m">
          <div>
            You are sharing {props.selectedItems.length} {props.selectedItems.length === 1 ? 'meeting' : 'meetings'}.
          </div>
          <FormField label="Recipients">
            <Multiselect
              selectedOptions={meetingRecipients.map((r) => ({ label: r, value: r }))}
              onChange={({ detail }) => setMeetingRecipients(detail.selectedOptions.map((o) => o.value))}
              options={meetingRecipients.map((r) => ({ label: r, value: r }))}
              removeSelectedOptions
              placeholder="No recipients added"
            />
          </FormField>
          <FormField label="Add recipient">
            <SpaceBetween direction="horizontal" size="xs">
              <Input
                value={newRecipients}
                onChange={(event) => setNewRecipients(event.detail.value)}
                placeholder="Enter email address"
              />
              <Button onClick={handleAddRecipients}>Add</Button>
            </SpaceBetween>
          </FormField>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
};
