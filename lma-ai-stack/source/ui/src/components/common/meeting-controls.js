/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT License.
 * See the LICENSE file in the project root for full license information.
 */
import { API } from 'aws-amplify';
import React, { useEffect, useState } from 'react';
import { useParams, useHistory } from 'react-router-dom';
import {
  Alert,
  Button,
  Header,
  Modal,
  TokenGroup,
  Container,
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
    const result = await invokeShareMeetings(props, allRecipients.join(','));
    setCurrentRecipients([]);
    setNewRecipients([]);
    setSubmit(false);
    setShareResult(result);
    await openShareSettings();
  };

  const showCurrentRecipients = () => {
    if (currentRecipients.length === 0) {
      const placeholder = originalCount === 0 ? 'None' : 'You have removed all current users';
      return (
        <FormField>
          <div>{placeholder}</div>
        </FormField>
      );
    }

    return (
      <FormField>
        <TokenGroup
          items={currentRecipients.map((r) => ({ label: r, value: r }))}
          onDismiss={({ detail: { itemIndex } }) => {
            const updatedRecipients = currentRecipients.filter((_, index) => index !== itemIndex);
            setCurrentRecipients(updatedRecipients);
            setChanged(currentRecipients.length !== 0);
            setShareResult(null);
          }}
          alignment="horizontal"
        />
      </FormField>
    );
  };

  const showNewRecipients = () => {
    return (
      <TokenGroup
        items={newRecipients.map((r) => ({ label: r, value: r }))}
        onDismiss={({ detail: { itemIndex } }) => {
          const updatedRecipients = newRecipients.filter((_, index) => index !== itemIndex);
          setNewRecipients(updatedRecipients);
        }}
        alignment="horizontal"
      />
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
          <Container header={<Header description={currentRecipientsDescription}>Existing Users</Header>}>
            {showCurrentRecipients()}
          </Container>
          <Container
            header={
              <Header description="Enter a comma-separated list of email addresses and choose Add.">Add Users</Header>
            }
          >
            <form onSubmit={handleAddRecipients}>
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
            </form>
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
  const [deleteResult, setDeleteResult] = useState(null);
  const [deletedCallIds, setDeletedCallIds] = useState([]);

  const history = useHistory();
  const { callId } = useParams();
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
    setDeleteResult(null);
    setDeletedCallIds([]);
  };

  const closeDeleteSettings = () => {
    setDeleteDisabled(false);
    setVisible(false);
    setDeleteResult(null);
    setDeletedCallIds([]);
    if (callId) {
      history.goBack();
    }
  };

  const handleDelete = async (e) => {
    console.log('callID', callId);
    e.preventDefault();
    setDeleteDisabled(true);
    setDeletedCallIds(props.selectedItems.map((c) => c.callId));
    const result = await invokeDeleteMeetings(props);
    setDeleteResult(result);
  };

  const handleDeleteSubmit = (event) => {
    event.preventDefault();
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
      {props.selectedItems.length > 0 ? (
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
                <ColumnLayout columns={1}>
                  <Input
                    placeholder={deleteConsentText}
                    onChange={(event) => setDeleteInputText(event.detail.value)}
                    value={deleteInputText}
                    ariaRequired
                  />
                  <Alert type="success" visible={deleteResult}>
                    {deleteResult}
                  </Alert>
                </ColumnLayout>
              </FormField>
            </form>
          </SpaceBetween>
        </Modal>
      ) : (
        <Modal
          visible={visible}
          onDismiss={closeDeleteSettings}
          header={
            <h3>
              Delete {deletedCallIds.length === 1 ? `"${deletedCallIds[0]}"` : `${deletedCallIds.length} meetings`}
            </h3>
          }
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
          <Alert type="success" visible={deleteResult}>
            {deleteResult}
          </Alert>
        </Modal>
      )}
    </SpaceBetween>
  );
};
