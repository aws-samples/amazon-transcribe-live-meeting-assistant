import React, { useState } from 'react';
import { Box, Button, Spinner, Header, Grid, Container, SpaceBetween, Input } from '@awsui/components-react';
import PropTypes from 'prop-types';
import { API, Logger } from 'aws-amplify';
import queryKnowledgeBase from '../../graphql/queries/queryKnowledgeBase';

const logger = new Logger('queryKnowledgeBase');

const ValueWithLabel = ({ label, index, children }) => (
  <>
    <Box variant="awsui-key-label">
      <span tabIndex={index}>{label}</span>
    </Box>
    {children}
  </>
);

ValueWithLabel.propTypes = {
  label: PropTypes.string.isRequired,
  index: PropTypes.number.isRequired,
  children: PropTypes.node.isRequired,
};

export const MeetingsQueryLayout = () => {
  const [inputQuery, setInputQuery] = useState('');
  const [meetingKbQueries, setMeetingKbQueries] = useState([]);
  const [meetingKbQueryStatus, setMeetingKbQueryStatus] = useState(false);

  const getElementByIdAsync = (id) =>
    new Promise((resolve) => {
      const getElement = () => {
        const element = document.getElementById(id);
        if (element) {
          resolve(element);
        } else {
          requestAnimationFrame(getElement);
        }
      };
      getElement();
    });

  const scrollToBottomOfChat = async () => {
    const chatDiv = await getElementByIdAsync('chatDiv');
    chatDiv.scrollTop = chatDiv.scrollHeight + 200;
  };

  const getMeetingsQueryResponseFromKB = async (input) => {
    const response = await API.graphql({
      query: queryKnowledgeBase,
      variables: { input },
    });
    return response;
  };

  const submitQuery = (query) => {
    if (meetingKbQueryStatus === true) {
      return;
    }

    setMeetingKbQueryStatus(true);

    const responseData = {
      label: query,
      value: '...',
    };
    const currentQueries = meetingKbQueries.concat(responseData);
    setMeetingKbQueries(currentQueries);
    scrollToBottomOfChat();

    logger.debug('Submitting GraphQL query:', query);
    const queryResponse = getMeetingsQueryResponseFromKB(query);

    queryResponse.then((r) => {
      const kbResponse = JSON.parse(r.data.queryKnowledgeBase);
      const kbanswer = kbResponse.output.text;
      const queries = currentQueries.map((q) => {
        if (q.value !== '...') {
          return q;
        }
        return {
          label: q.label,
          value: kbanswer,
        };
      });
      setMeetingKbQueries(queries);
      scrollToBottomOfChat();
    });
    setMeetingKbQueryStatus(false);
  };

  const onSubmit = (e) => {
    submitQuery(inputQuery);
    setInputQuery('');
    e.preventDefault();
    return true;
  };

  return (
    <Container
      fitHeight={false}
      header={<Header variant="h2">Meetings Knowledge Base Query Tool</Header>}
      /* For future use. :) */
      footer={
        <form onSubmit={onSubmit}>
          <Grid gridDefinition={[{ colspan: { default: 12, xxs: 9 } }, { default: 12, xxs: 3 }]}>
            <Input
              placeholder="Enter a question to query your meeting transcripts knowledge base."
              onChange={({ detail }) => setInputQuery(detail.value)}
              value={inputQuery}
            />
            <Button type="submit">Submit</Button>
          </Grid>
        </form>
      }
    >
      <div id="chatDiv" style={{ overflow: 'hidden', overflowY: 'auto', height: '30em' }}>
        <SpaceBetween size="m">
          {meetingKbQueries.length > 0 ? (
            meetingKbQueries.map((entry, i) => (
              // eslint-disable-next-line react/no-array-index-key
              <ValueWithLabel key={i} index={i} label={entry.label}>
                {entry.value === '...' ? (
                  <div style={{ height: '30px' }}>
                    <Spinner />
                  </div>
                ) : (
                  entry.value
                )}
              </ValueWithLabel>
            ))
          ) : (
            <ValueWithLabel key="nosummary">Ask a question below.</ValueWithLabel>
          )}
        </SpaceBetween>
      </div>
    </Container>
  );
};

export default MeetingsQueryLayout;
