import React from 'react';
import PropTypes from 'prop-types';
import {
  Container,
  Header,
  ColumnLayout,
  SpaceBetween,
  Box,
  ProgressBar,
  Badge,
  Alert,
  Icon,
} from '@awsui/components-react';

const formatDuration = (milliseconds) => {
  if (!milliseconds || milliseconds <= 0) return 'N/A';

  const seconds = Math.floor(milliseconds / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);

  if (hours > 0) {
    return `${hours}h ${minutes % 60}m ${seconds % 60}s`;
  }
  if (minutes > 0) {
    return `${minutes}m ${seconds % 60}s`;
  }
  return `${seconds}s`;
};

const formatPercentage = (value) => {
  if (value === null || value === undefined) return 'N/A';
  return `${(value * 100).toFixed(1)}%`;
};

const getTimeToJoinBadge = (timeToJoin) => {
  if (timeToJoin <= 30000) return { color: 'green', text: 'Fast' };
  if (timeToJoin <= 60000) return { color: 'blue', text: 'Normal' };
  return { color: 'red', text: 'Slow' };
};

const getUptimeBadge = (uptime) => {
  if (uptime >= 0.95) return { color: 'green', text: 'Excellent' };
  if (uptime >= 0.9) return { color: 'blue', text: 'Good' };
  return { color: 'red', text: 'Poor' };
};

const getLatencyBadge = (latency) => {
  if (latency <= 100) return { color: 'green', text: 'Low' };
  if (latency <= 200) return { color: 'blue', text: 'Medium' };
  return { color: 'red', text: 'High' };
};

const getAudioQualityBadge = (quality) => {
  if (quality >= 0.8) return { color: 'green', text: 'High' };
  if (quality >= 0.6) return { color: 'blue', text: 'Medium' };
  return { color: 'red', text: 'Low' };
};

const getStabilityStatus = (stability) => {
  if (stability >= 0.95) return 'success';
  if (stability >= 0.9) return 'in-progress';
  return 'error';
};

const getUptimeStatus = (uptime) => {
  if (uptime >= 0.95) return 'success';
  if (uptime >= 0.9) return 'in-progress';
  return 'error';
};

const PerformanceMetrics = ({ vpDetails }) => {
  const { metrics, connectionDetails } = vpDetails;

  if (!metrics && !connectionDetails) {
    return (
      <Container header={<Header variant="h3">Performance Metrics</Header>}>
        <Box textAlign="center" color="text-body-secondary" padding="l">
          No performance metrics available
        </Box>
      </Container>
    );
  }

  return (
    <Container header={<Header variant="h3">Performance Metrics</Header>}>
      <SpaceBetween direction="vertical" size="l">
        {/* Key Performance Indicators */}
        <ColumnLayout columns={4} variant="text-grid">
          <SpaceBetween size="xs">
            <Box color="text-label" fontWeight="bold">
              <Icon name="status-positive" /> Time to Join
            </Box>
            <div>
              {metrics?.timeToJoin ? (
                <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                  <span>{formatDuration(metrics.timeToJoin)}</span>
                  <Badge color={getTimeToJoinBadge(metrics.timeToJoin).color}>
                    {getTimeToJoinBadge(metrics.timeToJoin).text}
                  </Badge>
                </SpaceBetween>
              ) : (
                'N/A'
              )}
            </div>
          </SpaceBetween>

          <SpaceBetween size="xs">
            <Box color="text-label" fontWeight="bold">
              <Icon name="status-info" /> Uptime
            </Box>
            <div>
              {metrics?.uptime !== undefined ? (
                <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                  <span>{formatPercentage(metrics.uptime)}</span>
                  <Badge color={getUptimeBadge(metrics.uptime).color}>{getUptimeBadge(metrics.uptime).text}</Badge>
                </SpaceBetween>
              ) : (
                'N/A'
              )}
            </div>
          </SpaceBetween>

          <SpaceBetween size="xs">
            <Box color="text-label" fontWeight="bold">
              <Icon name="notification" /> Network Latency
            </Box>
            <div>
              {connectionDetails?.networkLatency ? (
                <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                  <span>{connectionDetails.networkLatency.toFixed()}ms</span>
                  <Badge color={getLatencyBadge(connectionDetails.networkLatency).color}>
                    {getLatencyBadge(connectionDetails.networkLatency).text}
                  </Badge>
                </SpaceBetween>
              ) : (
                'N/A'
              )}
            </div>
          </SpaceBetween>

          <SpaceBetween size="xs">
            <Box color="text-label" fontWeight="bold">
              <Icon name="microphone" /> Audio Quality
            </Box>
            <div>
              {connectionDetails?.audioQuality ? (
                <SpaceBetween direction="horizontal" size="xs" alignItems="center">
                  <span>{formatPercentage(connectionDetails.audioQuality)}</span>
                  <Badge color={getAudioQualityBadge(connectionDetails.audioQuality).color}>
                    {getAudioQualityBadge(connectionDetails.audioQuality).text}
                  </Badge>
                </SpaceBetween>
              ) : (
                'N/A'
              )}
            </div>
          </SpaceBetween>
        </ColumnLayout>

        {/* Progress Bars for Visual Metrics */}
        <ColumnLayout columns={2}>
          <SpaceBetween size="s">
            <Box fontWeight="bold">Connection Stability</Box>
            {connectionDetails?.connectionStability !== undefined ? (
              <ProgressBar
                value={connectionDetails.connectionStability * 100}
                additionalInfo={formatPercentage(connectionDetails.connectionStability)}
                description="Percentage of stable connection time"
                status={getStabilityStatus(connectionDetails.connectionStability)}
              />
            ) : (
              <Box color="text-body-secondary">No stability data available</Box>
            )}
          </SpaceBetween>

          <SpaceBetween size="s">
            <Box fontWeight="bold">Overall Uptime</Box>
            {metrics?.uptime !== undefined ? (
              <ProgressBar
                value={metrics.uptime * 100}
                additionalInfo={formatPercentage(metrics.uptime)}
                description="Percentage of time actively connected"
                status={getUptimeStatus(metrics.uptime)}
              />
            ) : (
              <Box color="text-body-secondary">No uptime data available</Box>
            )}
          </SpaceBetween>
        </ColumnLayout>

        {/* Additional Metrics */}
        <ColumnLayout columns={3} variant="text-grid">
          <SpaceBetween size="xs">
            <Box color="text-label" fontWeight="bold">
              Total Duration
            </Box>
            <div>{formatDuration(metrics?.totalDuration)}</div>
          </SpaceBetween>

          <SpaceBetween size="xs">
            <Box color="text-label" fontWeight="bold">
              Join Attempts
            </Box>
            <div>
              {connectionDetails?.joinAttempts || 0}
              {connectionDetails?.successfulJoins !== undefined && (
                <Box color="text-body-secondary" fontSize="body-s">
                  ({connectionDetails.successfulJoins} successful)
                </Box>
              )}
            </div>
          </SpaceBetween>

          <SpaceBetween size="xs">
            <Box color="text-label" fontWeight="bold">
              Transcript Segments
            </Box>
            <div>{metrics?.transcriptSegments || 0}</div>
          </SpaceBetween>

          <SpaceBetween size="xs">
            <Box color="text-label" fontWeight="bold">
              Audio Captured
            </Box>
            <div>{metrics?.audioMinutes ? `${metrics.audioMinutes.toFixed(1)} minutes` : 'N/A'}</div>
          </SpaceBetween>

          <SpaceBetween size="xs">
            <Box color="text-label" fontWeight="bold">
              Last Activity
            </Box>
            <div>{metrics?.lastActivity ? new Date(metrics.lastActivity).toLocaleString() : 'N/A'}</div>
          </SpaceBetween>

          <SpaceBetween size="xs">
            <Box color="text-label" fontWeight="bold">
              Connection Duration
            </Box>
            <div>{formatDuration(connectionDetails?.connectionDuration)}</div>
          </SpaceBetween>
        </ColumnLayout>

        {/* Performance Alerts */}
        {metrics?.uptime !== undefined && metrics.uptime < 0.9 && (
          <Alert type="warning">
            <SpaceBetween direction="vertical" size="s">
              <div>
                <strong>Low Uptime Detected</strong>
              </div>
              <div>
                This Virtual Participant had {formatPercentage(metrics.uptime)} uptime, which is below the recommended
                90% threshold. This may indicate connection stability issues or platform problems.
              </div>
            </SpaceBetween>
          </Alert>
        )}

        {connectionDetails?.networkLatency && connectionDetails.networkLatency > 500 && (
          <Alert type="warning">
            <SpaceBetween direction="vertical" size="s">
              <div>
                <strong>High Network Latency</strong>
              </div>
              <div>
                Network latency of {connectionDetails.networkLatency.toFixed()}ms detected. This may affect audio
                quality and real-time performance.
              </div>
            </SpaceBetween>
          </Alert>
        )}

        {connectionDetails?.audioQuality && connectionDetails.audioQuality < 0.6 && (
          <Alert type="error">
            <SpaceBetween direction="vertical" size="s">
              <div>
                <strong>Poor Audio Quality</strong>
              </div>
              <div>
                Audio quality is {formatPercentage(connectionDetails.audioQuality)}, which may result in poor transcript
                accuracy. Consider checking network bandwidth and audio settings.
              </div>
            </SpaceBetween>
          </Alert>
        )}
      </SpaceBetween>
    </Container>
  );
};

PerformanceMetrics.propTypes = {
  vpDetails: PropTypes.shape({
    metrics: PropTypes.shape({
      totalDuration: PropTypes.number,
      timeToJoin: PropTypes.number,
      uptime: PropTypes.number,
      averageLatency: PropTypes.number,
      transcriptSegments: PropTypes.number,
      audioMinutes: PropTypes.number,
      lastActivity: PropTypes.string,
    }),
    connectionDetails: PropTypes.shape({
      joinAttempts: PropTypes.number,
      successfulJoins: PropTypes.number,
      lastJoinAttempt: PropTypes.string,
      connectionDuration: PropTypes.number,
      disconnectionReason: PropTypes.string,
      networkLatency: PropTypes.number,
      audioQuality: PropTypes.number,
      connectionStability: PropTypes.number,
    }),
  }).isRequired,
};

export default PerformanceMetrics;
