/*
 * Copyright (c) 2025 Amazon.com
 * This file is licensed under the MIT-0 License.
 * See the LICENSE file in the project root for full license information.
 */
/**
 * All consents (admin): every consent given in advance on the deployment, for
 * Admins, on the My consents page. Listed and filtered on the server a page at
 * a time (listAllAdvanceConsents); a consent can be withdrawn on the person's
 * behalf, with the effect of their own withdrawal, and a person's consent
 * records can be permanently deleted, for an erasure request. The server
 * refuses all three to anyone but an Admin. See
 * docs/meeting-consent.md#administering-consents-given-in-advance.
 */
import React, { useCallback, useEffect, useState } from 'react';
import PropTypes from 'prop-types';
import { generateClient } from 'aws-amplify/api';
import { ConsoleLogger } from 'aws-amplify/utils';
import {
  Alert,
  Box,
  Button,
  Checkbox,
  DatePicker,
  FormField,
  Header,
  Input,
  Modal,
  Pagination,
  Select,
  SpaceBetween,
  Table,
  Textarea,
} from '@cloudscape-design/components';

import {
  deleteAdvanceConsentRecords,
  listAllAdvanceConsents,
  withdrawAdvanceConsentAsAdmin,
} from '../../graphql/queries/advanceConsent';
import { consentScopeLabel, consentStatusLabel } from '../common/advance-consent';
import { statusCell, textCell, when } from './consent-cells';

const client = generateClient();
const logger = new ConsoleLogger('AllConsentsView');

const errorText = (error, fallback) => error?.errors?.[0]?.message || error?.message || fallback;

export const PAGE_SIZE = 50;
/** The most pages a CSV export reads, at 100 consents a page. */
export const MAX_EXPORT_PAGES = 50;

const ANY = { value: '', label: 'Any' };
export const SCOPE_OPTIONS = [
  ANY,
  { value: 'meeting', label: 'One meeting' },
  { value: 'organizer', label: "An organizer's meetings" },
  { value: 'deployment', label: 'Every meeting' },
];
export const STATE_OPTIONS = [
  ANY,
  { value: 'active', label: 'In force' },
  { value: 'expired', label: 'Expired' },
  { value: 'withdrawn', label: 'Withdrawn' },
];

/** The listAllAdvanceConsents variables for the filters, without the empty ones. */
export const filterVariables = ({ person, scope, state, expiringBefore }) => {
  const out = {};
  if (person.trim()) out.Person = person.trim();
  if (scope) out.Scope = scope;
  if (state) out.State = state;
  if (expiringBefore) out.ExpiringBefore = expiringBefore;
  return out;
};

const csvCell = (value) => {
  const text = value === null || value === undefined ? '' : String(value);
  // A leading =, +, - or @ would be read as a formula by a spreadsheet.
  const safe = /^[=+\-@]/.test(text) ? `'${text}` : text;
  return /[",\n\r]/.test(safe) ? `"${safe.replace(/"/g, '""')}"` : safe;
};

export const CSV_COLUMNS = [
  ['Email', (c) => c.Email],
  ['Display name', (c) => c.DisplayName],
  ['Consent id', (c) => c.ConsentId],
  ['Scope', (c) => c.Scope],
  ['Covers', (c) => consentScopeLabel(c)],
  ['Organizer', (c) => c.Organizer],
  ['Label', (c) => (c.LabelKey ? `${c.LabelKey}=${c.LabelValue || ''}` : '')],
  ['Status', (c) => consentStatusLabel(c)],
  ['Given at', (c) => c.GrantedAt],
  ['Valid until', (c) => c.ValidUntil],
  ['Withdrawn at', (c) => c.WithdrawnAt],
  ['Withdrawn reason', (c) => c.WithdrawnReason],
  ['Withdrawn by', (c) => c.WithdrawnBy],
  ['Text version', (c) => c.NoticeVersion],
  ['Text SHA-256', (c) => c.NoticeHash],
  ['Counted in meetings', (c) => (c.CountedIn || []).map((m) => m.CallId).join('; ')],
];

/** The consents as CSV, one row each, with a header row. */
export const consentsCsv = (consents) =>
  [CSV_COLUMNS.map(([name]) => name), ...consents.map((c) => CSV_COLUMNS.map(([, value]) => value(c)))]
    .map((row) => row.map(csvCell).join(','))
    .join('\r\n');

const download = (text, name) => {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
};

const personCell = (c) => (
  <SpaceBetween size="xxxs">
    <Box>{c.Email}</Box>
    {c.DisplayName && (
      <Box color="text-body-secondary" fontSize="body-s">
        {c.DisplayName}
      </Box>
    )}
  </SpaceBetween>
);

const ConsentDetails = ({ consent, onDismiss }) => (
  <Modal visible onDismiss={onDismiss} header="Consent given in advance" size="large">
    <SpaceBetween size="m">
      <FormField label="Person">
        <Box>{consent.DisplayName ? `${consent.DisplayName} <${consent.Email}>` : consent.Email}</Box>
      </FormField>
      <FormField label="What it covers">
        <Box>{consentScopeLabel(consent)}</Box>
      </FormField>
      <FormField label="Status">{statusCell(consent)}</FormField>
      {consent.WithdrawnBy && (
        <FormField label="Withdrawn by">
          <Box>{consent.WithdrawnBy}</Box>
        </FormField>
      )}
      <FormField label="Given, and counts until">
        <Box>{`${when(consent.GrantedAt)} – ${when(consent.ValidUntil)}`}</Box>
      </FormField>
      <FormField label="Text agreed to">{textCell(consent)}</FormField>
      <FormField label="Meetings that counted it">
        <Box>
          {(consent.CountedIn || []).length
            ? consent.CountedIn.map((m) => `${m.CallId} (${when(m.At)})`).join(', ')
            : 'None'}
        </Box>
      </FormField>
      {(consent.Supersedes || []).length > 0 && (
        <FormField label="Replaced the earlier consents">
          <Box>{consent.Supersedes.join(', ')}</Box>
        </FormField>
      )}
      <FormField label="Consent id">
        <Box variant="code">{consent.ConsentId}</Box>
      </FormField>
    </SpaceBetween>
  </Modal>
);

ConsentDetails.propTypes = {
  consent: PropTypes.shape({
    ConsentId: PropTypes.string,
    Email: PropTypes.string,
    DisplayName: PropTypes.string,
    WithdrawnBy: PropTypes.string,
    GrantedAt: PropTypes.string,
    ValidUntil: PropTypes.string,
    CountedIn: PropTypes.arrayOf(PropTypes.shape({ CallId: PropTypes.string, At: PropTypes.string })),
    Supersedes: PropTypes.arrayOf(PropTypes.string),
  }).isRequired,
  onDismiss: PropTypes.func.isRequired,
};

const AllConsentsView = () => {
  const [person, setPerson] = useState('');
  const [scope, setScope] = useState(ANY);
  const [state, setState] = useState(ANY);
  const [expiringBefore, setExpiringBefore] = useState('');
  const [applied, setApplied] = useState({});
  const [pages, setPages] = useState([]);
  const [nextToken, setNextToken] = useState(null);
  const [pageIndex, setPageIndex] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState(null);
  const [selected, setSelected] = useState([]);
  const [details, setDetails] = useState(null);
  const [withdrawing, setWithdrawing] = useState(null);
  const [note, setNote] = useState('');
  const [deleting, setDeleting] = useState(null);
  const [understood, setUnderstood] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState('');
  const [exporting, setExporting] = useState(false);

  const fetchPage = useCallback(async (variables, token) => {
    const response = await client.graphql({
      query: listAllAdvanceConsents,
      variables: { ...variables, Limit: PAGE_SIZE, ...(token ? { NextToken: token } : {}) },
    });
    return response?.data?.listAllAdvanceConsents || { Consents: [], NextToken: null };
  }, []);

  const search = useCallback(
    async (variables) => {
      setLoading(true);
      setError('');
      setSelected([]);
      try {
        const page = await fetchPage(variables, null);
        setApplied(variables);
        setPages([page.Consents || []]);
        setNextToken(page.NextToken || null);
        setPageIndex(1);
      } catch (e) {
        logger.error('could not list the consents', e);
        setError(errorText(e, 'The consents could not be listed.'));
      } finally {
        setLoading(false);
      }
    },
    [fetchPage],
  );

  useEffect(() => {
    search({});
  }, [search]);

  const apply = () => search(filterVariables({ person, scope: scope.value, state: state.value, expiringBefore }));

  const nextPage = async () => {
    if (pageIndex < pages.length) {
      setPageIndex(pageIndex + 1);
      return;
    }
    if (!nextToken) return;
    setLoading(true);
    try {
      const page = await fetchPage(applied, nextToken);
      setPages((current) => [...current, page.Consents || []]);
      setNextToken(page.NextToken || null);
      setPageIndex(pages.length + 1);
    } catch (e) {
      logger.error('could not read the next page', e);
      setError(errorText(e, 'The next page could not be read.'));
    } finally {
      setLoading(false);
    }
  };

  const exportCsv = async () => {
    setExporting(true);
    setError('');
    try {
      const all = [];
      let token = null;
      for (let page = 0; page < MAX_EXPORT_PAGES; page += 1) {
        // One page after another: each needs the token of the one before.
        // eslint-disable-next-line no-await-in-loop
        const response = await client.graphql({
          query: listAllAdvanceConsents,
          variables: { ...applied, Limit: 100, ...(token ? { NextToken: token } : {}) },
        });
        const answer = response?.data?.listAllAdvanceConsents || {};
        all.push(...(answer.Consents || []));
        token = answer.NextToken || null;
        if (!token) break;
      }
      download(consentsCsv(all), `consents-given-in-advance-${new Date().toISOString().slice(0, 10)}.csv`);
      setResult({
        type: token ? 'warning' : 'success',
        text: token
          ? `Exported the first ${all.length} consents; narrow the filters to export the rest.`
          : `Exported ${all.length} consents.`,
      });
    } catch (e) {
      logger.error('could not export the consents', e);
      setError(errorText(e, 'The consents could not be exported.'));
    } finally {
      setExporting(false);
    }
  };

  const target = selected.length === 1 ? selected[0] : null;

  const withdraw = async () => {
    setBusy(true);
    setActionError('');
    try {
      await client.graphql({
        query: withdrawAdvanceConsentAsAdmin,
        variables: {
          input: { Email: withdrawing.Email, ConsentId: withdrawing.ConsentId, ...(note.trim() ? { Note: note } : {}) },
        },
      });
      setResult({
        type: 'success',
        text:
          `The consent of ${withdrawing.Email} is withdrawn. A meeting in progress that counted it records an ` +
          'objection and stops capture within about 10 seconds.',
      });
      setWithdrawing(null);
      await search(applied);
    } catch (e) {
      logger.error('could not withdraw the consent', e);
      setActionError(errorText(e, 'The consent could not be withdrawn.'));
    } finally {
      setBusy(false);
    }
  };

  const erase = async () => {
    setBusy(true);
    setActionError('');
    try {
      const input = deleting.all
        ? { Email: deleting.consent.Email, AllForPerson: true }
        : { Email: deleting.consent.Email, ConsentIds: [deleting.consent.ConsentId] };
      const response = await client.graphql({ query: deleteAdvanceConsentRecords, variables: { input } });
      const answer = response?.data?.deleteAdvanceConsentRecords || {};
      setResult({
        type: 'success',
        text:
          `Deleted ${answer.Deleted} consent record${answer.Deleted === 1 ? '' : 's'} of ${deleting.consent.Email}` +
          `${answer.Withdrawn ? `, ${answer.Withdrawn} of them withdrawn first` : ''}. Audit entry ${answer.AuditId}.`,
      });
      setDeleting(null);
      await search(applied);
    } catch (e) {
      logger.error('could not delete the consent records', e);
      setActionError(errorText(e, 'The consent records could not be deleted.'));
    } finally {
      setBusy(false);
    }
  };

  const openWithdraw = () => {
    setNote('');
    setActionError('');
    setWithdrawing(target);
  };
  const openDelete = (all) => {
    setUnderstood(false);
    setActionError('');
    setDeleting({ consent: target, all });
  };

  const columns = [
    {
      id: 'person',
      header: 'Person',
      cell: personCell,
      isRowHeader: true,
    },
    { id: 'scope', header: 'What it covers', cell: (c) => consentScopeLabel(c) },
    { id: 'status', header: 'Status', cell: (c) => statusCell(c) },
    { id: 'given', header: 'Given', cell: (c) => when(c.GrantedAt) },
    { id: 'until', header: 'Counts until', cell: (c) => when(c.ValidUntil) },
    {
      id: 'text',
      header: 'Text',
      cell: (c) => `Version ${c.NoticeVersion}; SHA-256 ${(c.NoticeHash || '').slice(0, 12)}…`,
    },
    { id: 'counted', header: 'Counted in', cell: (c) => `${(c.CountedIn || []).length} meetings` },
  ];

  const items = pages[pageIndex - 1] || [];
  return (
    <SpaceBetween size="l">
      {result && (
        <Alert type={result.type} dismissible onDismiss={() => setResult(null)}>
          {result.text}
        </Alert>
      )}
      {error && <Alert type="error">{error}</Alert>}
      <Table
        header={
          <Header
            variant="h2"
            description="Filters apply on the server. Withdrawing or deleting acts on the one consent selected."
            actions={
              <SpaceBetween direction="horizontal" size="xs">
                <Button disabled={!target} onClick={() => setDetails(target)}>
                  View details
                </Button>
                <Button
                  disabled={!target || target.Status !== 'active'}
                  disabledReason={target ? 'Only a consent in force can be withdrawn' : 'Select a consent'}
                  onClick={openWithdraw}
                >
                  Withdraw
                </Button>
                <Button disabled={!target} onClick={() => openDelete(false)}>
                  Delete permanently
                </Button>
                <Button disabled={!target} onClick={() => openDelete(true)}>
                  Delete all for this person
                </Button>
                <Button iconName="download" onClick={exportCsv} loading={exporting}>
                  Export CSV
                </Button>
              </SpaceBetween>
            }
          >
            Consents given in advance
          </Header>
        }
        filter={
          <SpaceBetween direction="horizontal" size="s" alignItems="end">
            <FormField label="Person">
              <Input
                value={person}
                onChange={({ detail }) => setPerson(detail.value)}
                placeholder="Start of the email address"
                ariaLabel="Person"
                onKeyDown={({ detail }) => detail.key === 'Enter' && apply()}
              />
            </FormField>
            <FormField label="Scope">
              <Select
                selectedOption={scope}
                options={SCOPE_OPTIONS}
                onChange={({ detail }) => setScope(detail.selectedOption)}
              />
            </FormField>
            <FormField label="State">
              <Select
                selectedOption={state}
                options={STATE_OPTIONS}
                onChange={({ detail }) => setState(detail.selectedOption)}
              />
            </FormField>
            <FormField label="Counts until before">
              <DatePicker
                value={expiringBefore}
                onChange={({ detail }) => setExpiringBefore(detail.value)}
                placeholder="YYYY/MM/DD"
                ariaLabel="Counts until before"
              />
            </FormField>
            <Button onClick={apply} loading={loading}>
              Apply filters
            </Button>
          </SpaceBetween>
        }
        pagination={
          <Pagination
            currentPageIndex={pageIndex}
            pagesCount={Math.max(pages.length, 1)}
            openEnd={Boolean(nextToken)}
            onPreviousPageClick={() => setPageIndex(Math.max(1, pageIndex - 1))}
            onNextPageClick={nextPage}
            onChange={({ detail }) => setPageIndex(Math.min(detail.currentPageIndex, pages.length))}
          />
        }
        items={items}
        columnDefinitions={columns}
        loading={loading}
        loadingText="Loading consents"
        selectionType="single"
        selectedItems={selected}
        onSelectionChange={({ detail }) => setSelected(detail.selectedItems)}
        trackBy="ConsentId"
        empty={<Box color="text-body-secondary">No consents match the filters.</Box>}
      />
      {details && <ConsentDetails consent={details} onDismiss={() => setDetails(null)} />}
      <Modal
        visible={Boolean(withdrawing)}
        onDismiss={() => setWithdrawing(null)}
        header="Withdraw consent on the person's behalf"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setWithdrawing(null)} disabled={busy}>
                Cancel
              </Button>
              <Button variant="primary" onClick={withdraw} loading={busy}>
                Withdraw
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {withdrawing && (
          <SpaceBetween size="m">
            <Box>
              {`${consentScopeLabel(withdrawing)}, given by ${withdrawing.Email}. It stops counting at once, as when ` +
                'the person withdraws it themselves: a meeting in progress that counted it records their objection ' +
                'and stops capture. Their My consents page shows it was withdrawn by an administrator.'}
            </Box>
            <FormField
              label={
                <span>
                  Note to the person <i>- optional</i>
                </span>
              }
              constraintText="At most 500 characters. Shown on their My consents page."
            >
              <Textarea value={note} onChange={({ detail }) => setNote(detail.value)} ariaLabel="Note to the person" />
            </FormField>
            {actionError && <Alert type="error">{actionError}</Alert>}
          </SpaceBetween>
        )}
      </Modal>
      <Modal
        visible={Boolean(deleting)}
        onDismiss={() => setDeleting(null)}
        header="Permanently delete consent records"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleting(null)} disabled={busy}>
                Cancel
              </Button>
              <Button variant="primary" onClick={erase} loading={busy} disabled={!understood}>
                Delete permanently
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {deleting && (
          <SpaceBetween size="m">
            <Box>
              {deleting.all
                ? `Every consent record kept under ${deleting.consent.Email} will be deleted.`
                : `The consent record ${deleting.consent.ConsentId} of ${deleting.consent.Email} ` +
                  `(${consentScopeLabel(deleting.consent)}) will be deleted.`}
            </Box>
            <Box>
              A consent still in force is withdrawn first, so a meeting in progress that counted it stops capture. The
              records are then removed and cannot be restored. An audit entry records who deleted them, when, the
              address and the consent ids, without their content. Each meeting&apos;s own consent record is not changed:
              it stays with the meeting and follows the meeting&apos;s retention.
            </Box>
            <Checkbox checked={understood} onChange={({ detail }) => setUnderstood(detail.checked)}>
              I understand this cannot be undone.
            </Checkbox>
            {actionError && <Alert type="error">{actionError}</Alert>}
          </SpaceBetween>
        )}
      </Modal>
    </SpaceBetween>
  );
};

export default AllConsentsView;
