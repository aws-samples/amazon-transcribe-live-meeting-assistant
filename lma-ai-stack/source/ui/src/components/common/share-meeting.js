import { API } from 'aws-amplify';
import React, { useState } from 'react';
import { Alert, Button, Modal, SpaceBetween, FormField, Input, Form } from '@awsui/components-react';
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
  const [meetingRecipients, setMeetingRecipients] = React.useState('');
  const [submit, setSubmit] = useState(false);
  const [shareResult, setShareResult] = useState(null);

  const openShareSettings = () => {
    setShare(true);
  };

  const closeShareSettings = () => {
    setShare(false);
    setMeetingRecipients('');
    setShareResult(null);
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setSubmit(true);
    console.log('Meeting Recipients: ', meetingRecipients);
    const result = await shareMeetings(props, meetingRecipients);
    setMeetingRecipients('');
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
          <form
            onSubmit={(e) => {
              e.preventDefault();
              handleSubmit(e);
            }}
          >
            <Form
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button formAction="none" onClick={closeShareSettings}>
                    Close
                  </Button>
                  <Button
                    variant="primary"
                    disabled={submit || !meetingRecipients.trim()}
                    onclick={(e) => {
                      e.preventDefault();
                      handleSubmit(e);
                    }}
                  >
                    Submit
                  </Button>
                </SpaceBetween>
              }
            >
              <FormField>
                <Input value={meetingRecipients} onChange={(event) => setMeetingRecipients(event.detail.value)} />
              </FormField>
              <Alert type="info" visible={shareResult}>
                {shareResult}
              </Alert>
            </Form>
          </form>
        }
        header={<h3>Share Meeting</h3>}
      >
        You are sharing&#xA0;
        {props.selectedItems.length}
        {props.selectedItems.length === 1 ? ' meeting' : ' meetings'}
        &#x2e; Enter a comma separated list of email addresses.
      </Modal>
    </SpaceBetween>
  );
};
