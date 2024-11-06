import { API } from 'aws-amplify';
import React, { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Header,
  Modal,
  Container,
  Multiselect,
  SpaceBetween,
  FormField,
  Input,
  Form,
  Box,
  ColumnLayout,
} from '@awsui/components-react';
import shareMeetings from '../../graphql/queries/shareMeetings';
import deleteMeetings from '../../graphql/queries/deleteMeetings';

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

const callsWithKeys = (props) => {
  const { calls } = props;
  const callsKeys = props.selectedItems.map(({ callId }) => {
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
  return callsKeys;
};

const invokeShareMeetings = async (props, currentRecipients) => {
  const callsKeys = callsWithKeys(props);
  const response = await API.graphql({
    query: shareMeetings,
    variables: {
      input: { Calls: callsKeys, MeetingRecipients: currentRecipients },
    },
  });

  const result = response.data.shareMeetings.Result;
  return result;
};

const invokeDeleteMeetings = async (props) => {
  const callsKeys = callsWithKeys(props);
  const response = await API.graphql({
    query: deleteMeetings,
    variables: {
      input: { Calls: callsKeys },
    },
  });

  const result = response.data.deleteMeetings.Result;
  return result;
};

export const shareModal = (props) => {
  const [share, setShare] = useState(false);
  const [currentRecipients, setCurrentRecipients] = useState([]);
  const [newRecipients, setNewRecipients] = useState([]);
  const [addRecipients, setAddRecipients] = useState('');
  const [changed, setChanged] = useState(false);
  const [originalCount, setOriginalCount] = useState(0);
  const [submit, setSubmit] = useState(false);
  const [shareResult, setShareResult] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const modalContent =
    props.selectedItems.length === 1 ? `"${props.selectedItems[0].callId}"` : `${props.selectedItems.length} meetings`;

  const currentRecipientsDescription =
    props.selectedItems.length === 1
      ? `The following users have access to "${props.selectedItems[0].callId}". Remove users who no longer need access.`
      : `The following users have access to one or more of the selected meetings. If you share the meetings, all users in this list will have access to ${props.selectedItems.length} meetings. If you remove users, they will lose access to ${props.selectedItems.length} meetings.`;

  const { getCallDetailsFromCallIds } = props;

  const parseSharedWith = (sharedWithString) => {
    return (sharedWithString || '')
      .replace(/[[\]]/g, '')
      .split(',')
      .map((email) => email.trim())
      .filter((email) => email);
  };

  const openShareSettings = async () => {
    setShare(true);
    setIsLoading(true);
    const callDetails = await getCallDetailsFromCallIds(props.selectedItems.map((c) => c.callId));
    console.log('CALL SHARE MEETINGS RESPONSE:', callDetails);

    const recipients = new Set();
    callDetails.forEach((call) => {
      const sharedWithArray = parseSharedWith(call.SharedWith);
      sharedWithArray.forEach((email) => recipients.add(email));
    });

    const recipientList = Array.from(recipients).map((email) => email);
    setCurrentRecipients(recipientList);
    setIsLoading(false);
    setChanged(false);
    setOriginalCount(recipientList.length);
  };

  const closeShareSettings = () => {
    setShare(false);
    setCurrentRecipients([]);
    setAddRecipients('');
    setShareResult(null);
  };

  const handleAddRecipients = () => {
    if (addRecipients.trim()) {
      const emailList = addRecipients.split(',').map((email) => email.trim());
      const validEmails = emailList.filter((email) => {
        // Basic email validation regex
        const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
        return emailRegex.test(email) && !currentRecipients.includes(email) && !newRecipients.includes(email);
      });
      setNewRecipients([...newRecipients, ...validEmails]);
      setAddRecipients('');
    }
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setShareResult(null);
    setSubmit(true);
    // merge current and new recipients
    const allRecipients = [...new Set([...currentRecipients, ...newRecipients])];
    console.log('Meeting Recipients: ', allRecipients);
    const result = await invokeShareMeetings(props, allRecipients.join(','));
    setCurrentRecipients([]);
    setNewRecipients([]);
    setSubmit(false);
    setShareResult(result);
    await openShareSettings();
  };

  const showCurrentRecipients = () => {
    if (currentRecipients.length === 0) {
      const placeholder = originalCount === 0 ? 'No recipients' : 'You have removed all current recipients';
      return (
        <FormField>
          <div>{placeholder}</div>
        </FormField>
      );
    }

    return (
      <FormField>
        <Multiselect
          selectedOptions={currentRecipients.map((r) => ({ label: r, value: r }))}
          onChange={({ detail }) => {
            const updatedRecipients = detail.selectedOptions.map((o) => o.value);
            setCurrentRecipients(updatedRecipients);
            setChanged(currentRecipients.length !== 0);
            setShareResult(null);
          }}
          options={currentRecipients.map((r) => ({ label: r, value: r }))}
          removeSelectedOptions
          placeholder={`Currently shared with ${originalCount} recipient(s)`}
        />
      </FormField>
    );
  };

  const showNewRecipients = () => {
    return (
      <FormField>
        <Multiselect
          selectedOptions={newRecipients.map((r) => ({ label: r, value: r }))}
          onChange={({ detail }) => setNewRecipients(detail.selectedOptions.map((o) => o.value))}
          options={newRecipients.map((r) => ({ label: r, value: r }))}
          removeSelectedOptions
          placeholder="New recipients"
        />
      </FormField>
    );
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
        size="large"
        disableContentPaddings
        footer={
          <Form
            actions={
              <SpaceBetween direction="horizontal" size="xxs">
                <Button formAction="none" loading={isLoading} onClick={closeShareSettings}>
                  Close
                </Button>
                <Button
                  variant="primary"
                  loading={isLoading}
                  disabled={
                    submit ||
                    !changed ||
                    (currentRecipients.length === 0 && originalCount === 0 && newRecipients.length === 0)
                  }
                  onClick={handleSubmit}
                >
                  Submit
                </Button>
              </SpaceBetween>
            }
          >
            <Alert type="success" visible={shareResult}>
              {shareResult}
            </Alert>
          </Form>
        }
        header={<h3>Share {modalContent}</h3>}
      >
        <SpaceBetween size="s">
          <Container
            disableHeaderPaddings
            header={<Header description={currentRecipientsDescription}>Existing Users</Header>}
          >
            {showCurrentRecipients()}
          </Container>
          <Container
            disableHeaderPaddings
            header={
              <Header description="Enter a comma-separated list of email addresses and choose Add.">Add Users</Header>
            }
          >
            <FormField>
              <SpaceBetween direction="horizontal" size="xs">
                <Input
                  value={addRecipients}
                  onChange={(event) => {
                    setChanged(true);
                    setAddRecipients(event.detail.value);
                    setShareResult(null);
                  }}
                  placeholder="Enter email addresses"
                />
                <Button onClick={handleAddRecipients}>Add</Button>
              </SpaceBetween>
            </FormField>
            {showNewRecipients()}
          </Container>
        </SpaceBetween>
      </Modal>
    </SpaceBetween>
  );
};

export const deleteModal = (props) => {
  const [visible, setVisible] = useState(false);
  const [deleteDisabled, setDeleteDisabled] = useState(false);

  const deleteConsentText = 'confirm';
  const modalContent =
    props.selectedItems.length === 1 ? `"${props.selectedItems[0].callId}"` : `${props.selectedItems.length} meetings`;

  const [deleteInputText, setDeleteInputText] = useState('');
  const inputMatchesConsentText = deleteInputText.toLowerCase() === deleteConsentText;
  useEffect(() => {
    setDeleteInputText('');
  }, [visible]);

  const openDeleteSettings = async () => {
    setVisible(true);
  };

  const closeDeleteSettings = () => {
    setDeleteDisabled(false);
    setVisible(false);
  };

  const handleDelete = async (e) => {
    e.preventDefault();
    setDeleteDisabled(true);
    await invokeDeleteMeetings(props);
    console.log('IN HANDLE DELETE');
  };

  const handleDeleteSubmit = (event) => {
    event.preventDefault();
    console.log('IN HANDLE DELETE SUBMIT');
    if (inputMatchesConsentText) {
      handleDelete(event);
    }
  };

  return (
    <SpaceBetween size="xxs" direction="horizontal">
      <Button
        iconName="remove"
        variant="normal"
        loading={props.loading}
        disabled={props.selectedItems.length === 0}
        onClick={openDeleteSettings}
      />
      <Modal
        visible={visible}
        onDismiss={closeDeleteSettings}
        header={<h3>Delete {modalContent}</h3>}
        closeAriaLabel="Close dialog"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={closeDeleteSettings}>
                Close
              </Button>
              <Button
                variant="primary"
                onClick={handleDelete}
                disabled={!inputMatchesConsentText || deleteDisabled}
                data-testid="submit"
              >
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {props.selectedItems.length > 0 && (
          <SpaceBetween size="m">
            {props.selectedItems.length > 1 ? (
              <Box variant="span">
                Permanently delete{' '}
                <Box variant="span" fontWeight="bold">
                  {props.selectedItems.length} meetings
                </Box>
                ? You can’t undo this action.
              </Box>
            ) : (
              <Box variant="span">
                Permanently delete meeting{' '}
                <Box variant="span" fontWeight="bold">
                  {props.selectedItems[0].callId}
                </Box>
                ? You can’t undo this action.
              </Box>
            )}

            <Alert type="warning" statusIconAriaLabel="Warning">
              Proceeding with this action will delete the
              {props.selectedItems.length > 1
                ? ' meetings with all their content. '
                : ' meeting with all its content.'}{' '}
            </Alert>

            <Box>To avoid accidental deletions, we ask you to provide additional written consent.</Box>

            <form onSubmit={handleDeleteSubmit}>
              <FormField label={`To confirm this deletion, type "${deleteConsentText}".`}>
                <ColumnLayout columns={2}>
                  <Input
                    placeholder={deleteConsentText}
                    onChange={(event) => setDeleteInputText(event.detail.value)}
                    value={deleteInputText}
                    ariaRequired
                  />
                </ColumnLayout>
              </FormField>
            </form>
          </SpaceBetween>
        )}
      </Modal>
    </SpaceBetween>
  );
};
