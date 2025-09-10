"""
Virtual Participant Performance Monitor
Tracks and analyzes VP performance metrics and provides insights
"""

import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
import statistics

@dataclass
class PerformanceMetrics:
    """Performance metrics for a Virtual Participant"""
    total_duration: int = 0  # Total time from creation to completion (ms)
    time_to_join: Optional[int] = None  # Time to successfully join (ms)
    uptime: float = 0.0  # Percentage of time successfully connected
    average_latency: Optional[float] = None  # Average network latency (ms)
    transcript_segments: int = 0  # Number of transcript segments captured
    audio_minutes: float = 0.0  # Total audio captured in minutes
    last_activity: Optional[str] = None  # Last activity timestamp
    connection_stability: float = 0.0  # Connection stability score (0-1)
    audio_quality: float = 0.0  # Audio quality score (0-1)

@dataclass
class PerformanceInsight:
    """Performance insight with recommendations"""
    category: str
    severity: str  # 'info', 'warning', 'error'
    message: str
    recommendation: str
    metric_value: Optional[float] = None
    threshold: Optional[float] = None

class PerformanceMonitor:
    """
    Monitors and analyzes Virtual Participant performance
    """
    
    def __init__(self):
        # Performance thresholds
        self.thresholds = {
            'time_to_join_warning': 30000,  # 30 seconds
            'time_to_join_error': 60000,    # 1 minute
            'uptime_warning': 0.95,         # 95%
            'uptime_error': 0.90,           # 90%
            'latency_warning': 200,         # 200ms
            'latency_error': 500,           # 500ms
            'stability_warning': 0.95,      # 95%
            'stability_error': 0.90,        # 90%
            'audio_quality_warning': 0.8,   # 80%
            'audio_quality_error': 0.6      # 60%
        }
    
    def calculate_uptime(self, status_history: List[Dict], 
                        created_at: str, ended_at: str = None) -> float:
        """
        Calculate uptime percentage based on status history
        
        Args:
            status_history: List of status history entries
            created_at: VP creation timestamp
            ended_at: VP end timestamp (optional)
            
        Returns:
            Uptime percentage (0.0 to 1.0)
        """
        
        if not status_history:
            return 0.0
        
        # Parse timestamps
        start_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
        end_time = datetime.fromisoformat(
            (ended_at or datetime.now(timezone.utc).isoformat()).replace('Z', '+00:00')
        )
        
        total_duration = (end_time - start_time).total_seconds()
        
        if total_duration <= 0:
            return 0.0
        
        # Calculate time spent in active states
        active_states = {'JOINED', 'ACTIVE'}
        active_duration = 0
        
        for i, entry in enumerate(status_history):
            if entry['status'] in active_states:
                entry_start = datetime.fromisoformat(entry['timestamp'].replace('Z', '+00:00'))
                
                # Find the end time for this status
                if i + 1 < len(status_history):
                    entry_end = datetime.fromisoformat(
                        status_history[i + 1]['timestamp'].replace('Z', '+00:00')
                    )
                else:
                    entry_end = end_time
                
                active_duration += (entry_end - entry_start).total_seconds()
        
        return min(active_duration / total_duration, 1.0)
    
    def calculate_connection_stability(self, status_history: List[Dict]) -> float:
        """
        Calculate connection stability based on status transitions
        
        Args:
            status_history: List of status history entries
            
        Returns:
            Stability score (0.0 to 1.0)
        """
        
        if len(status_history) <= 1:
            return 1.0
        
        # Count problematic transitions
        problematic_transitions = 0
        total_transitions = len(status_history) - 1
        
        for i in range(len(status_history) - 1):
            current_status = status_history[i]['status']
            next_status = status_history[i + 1]['status']
            
            # Identify problematic transitions
            if (current_status in ['JOINED', 'ACTIVE'] and 
                next_status in ['CONNECTING', 'JOINING', 'FAILED']):
                problematic_transitions += 1
        
        if total_transitions == 0:
            return 1.0
        
        stability = 1.0 - (problematic_transitions / total_transitions)
        return max(stability, 0.0)
    
    def analyze_performance(self, vp_data: Dict) -> Dict[str, Any]:
        """
        Analyze Virtual Participant performance and generate insights
        
        Args:
            vp_data: Virtual Participant data dictionary
            
        Returns:
            Performance analysis with metrics and insights
        """
        
        status_history = vp_data.get('statusHistory', [])
        created_at = vp_data.get('createdAt')
        ended_at = vp_data.get('endedAt')
        connection_details = vp_data.get('connectionDetails', {})
        existing_metrics = vp_data.get('metrics', {})
        
        if not created_at:
            return {'error': 'Missing creation timestamp'}
        
        # Calculate performance metrics
        metrics = PerformanceMetrics()
        
        # Calculate total duration
        if ended_at:
            start_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
            end_time = datetime.fromisoformat(ended_at.replace('Z', '+00:00'))
            metrics.total_duration = int((end_time - start_time).total_seconds() * 1000)
        
        # Calculate time to join
        for entry in status_history:
            if entry['status'] == 'JOINED':
                start_time = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
                join_time = datetime.fromisoformat(entry['timestamp'].replace('Z', '+00:00'))
                metrics.time_to_join = int((join_time - start_time).total_seconds() * 1000)
                break
        
        # Calculate uptime
        metrics.uptime = self.calculate_uptime(status_history, created_at, ended_at)
        
        # Get connection metrics
        metrics.average_latency = connection_details.get('networkLatency')
        metrics.connection_stability = self.calculate_connection_stability(status_history)
        metrics.audio_quality = connection_details.get('audioQuality', 0.0)
        
        # Get existing metrics
        metrics.transcript_segments = existing_metrics.get('transcriptSegments', 0)
        metrics.audio_minutes = existing_metrics.get('audioMinutes', 0.0)
        metrics.last_activity = existing_metrics.get('lastActivity')
        
        # Generate insights
        insights = self._generate_insights(metrics, vp_data)
        
        # Calculate overall performance score
        performance_score = self._calculate_performance_score(metrics)
        
        return {
            'metrics': asdict(metrics),
            'insights': [asdict(insight) for insight in insights],
            'performanceScore': performance_score,
            'analysis': {
                'timeToJoinStatus': self._get_time_to_join_status(metrics.time_to_join),
                'uptimeStatus': self._get_uptime_status(metrics.uptime),
                'stabilityStatus': self._get_stability_status(metrics.connection_stability),
                'overallStatus': self._get_overall_status(performance_score)
            }
        }
    
    def _generate_insights(self, metrics: PerformanceMetrics, vp_data: Dict) -> List[PerformanceInsight]:
        """Generate performance insights and recommendations"""
        
        insights = []
        
        # Time to join analysis
        if metrics.time_to_join:
            if metrics.time_to_join > self.thresholds['time_to_join_error']:
                insights.append(PerformanceInsight(
                    category='connection',
                    severity='error',
                    message=f'Slow connection time: {metrics.time_to_join/1000:.1f}s',
                    recommendation='Check network connectivity and meeting platform status',
                    metric_value=metrics.time_to_join,
                    threshold=self.thresholds['time_to_join_error']
                ))
            elif metrics.time_to_join > self.thresholds['time_to_join_warning']:
                insights.append(PerformanceInsight(
                    category='connection',
                    severity='warning',
                    message=f'Moderate connection time: {metrics.time_to_join/1000:.1f}s',
                    recommendation='Monitor network conditions for optimal performance',
                    metric_value=metrics.time_to_join,
                    threshold=self.thresholds['time_to_join_warning']
                ))
        
        # Uptime analysis
        if metrics.uptime < self.thresholds['uptime_error']:
            insights.append(PerformanceInsight(
                category='reliability',
                severity='error',
                message=f'Low uptime: {metrics.uptime*100:.1f}%',
                recommendation='Investigate connection stability and platform reliability',
                metric_value=metrics.uptime,
                threshold=self.thresholds['uptime_error']
            ))
        elif metrics.uptime < self.thresholds['uptime_warning']:
            insights.append(PerformanceInsight(
                category='reliability',
                severity='warning',
                message=f'Moderate uptime: {metrics.uptime*100:.1f}%',
                recommendation='Monitor for intermittent connection issues',
                metric_value=metrics.uptime,
                threshold=self.thresholds['uptime_warning']
            ))
        
        # Latency analysis
        if metrics.average_latency:
            if metrics.average_latency > self.thresholds['latency_error']:
                insights.append(PerformanceInsight(
                    category='performance',
                    severity='error',
                    message=f'High latency: {metrics.average_latency:.0f}ms',
                    recommendation='Check network path and consider alternative connection',
                    metric_value=metrics.average_latency,
                    threshold=self.thresholds['latency_error']
                ))
            elif metrics.average_latency > self.thresholds['latency_warning']:
                insights.append(PerformanceInsight(
                    category='performance',
                    severity='warning',
                    message=f'Elevated latency: {metrics.average_latency:.0f}ms',
                    recommendation='Monitor network performance',
                    metric_value=metrics.average_latency,
                    threshold=self.thresholds['latency_warning']
                ))
        
        # Connection stability analysis
        if metrics.connection_stability < self.thresholds['stability_error']:
            insights.append(PerformanceInsight(
                category='reliability',
                severity='error',
                message=f'Unstable connection: {metrics.connection_stability*100:.1f}%',
                recommendation='Investigate network stability and platform issues',
                metric_value=metrics.connection_stability,
                threshold=self.thresholds['stability_error']
            ))
        elif metrics.connection_stability < self.thresholds['stability_warning']:
            insights.append(PerformanceInsight(
                category='reliability',
                severity='warning',
                message=f'Connection instability detected: {metrics.connection_stability*100:.1f}%',
                recommendation='Monitor for connection drops',
                metric_value=metrics.connection_stability,
                threshold=self.thresholds['stability_warning']
            ))
        
        # Audio quality analysis
        if metrics.audio_quality > 0:
            if metrics.audio_quality < self.thresholds['audio_quality_error']:
                insights.append(PerformanceInsight(
                    category='quality',
                    severity='error',
                    message=f'Poor audio quality: {metrics.audio_quality*100:.1f}%',
                    recommendation='Check audio settings and network bandwidth',
                    metric_value=metrics.audio_quality,
                    threshold=self.thresholds['audio_quality_error']
                ))
            elif metrics.audio_quality < self.thresholds['audio_quality_warning']:
                insights.append(PerformanceInsight(
                    category='quality',
                    severity='warning',
                    message=f'Suboptimal audio quality: {metrics.audio_quality*100:.1f}%',
                    recommendation='Monitor audio quality settings',
                    metric_value=metrics.audio_quality,
                    threshold=self.thresholds['audio_quality_warning']
                ))
        
        # Positive insights
        if not insights:
            insights.append(PerformanceInsight(
                category='overall',
                severity='info',
                message='Virtual Participant performed well',
                recommendation='Continue monitoring for consistent performance'
            ))
        
        return insights
    
    def _calculate_performance_score(self, metrics: PerformanceMetrics) -> float:
        """Calculate overall performance score (0.0 to 1.0)"""
        
        scores = []
        
        # Time to join score (inverse relationship)
        if metrics.time_to_join:
            if metrics.time_to_join <= 10000:  # 10 seconds
                scores.append(1.0)
            elif metrics.time_to_join <= 30000:  # 30 seconds
                scores.append(0.8)
            elif metrics.time_to_join <= 60000:  # 1 minute
                scores.append(0.6)
            else:
                scores.append(0.4)
        
        # Uptime score
        scores.append(metrics.uptime)
        
        # Connection stability score
        scores.append(metrics.connection_stability)
        
        # Latency score
        if metrics.average_latency:
            if metrics.average_latency <= 100:
                scores.append(1.0)
            elif metrics.average_latency <= 200:
                scores.append(0.8)
            elif metrics.average_latency <= 500:
                scores.append(0.6)
            else:
                scores.append(0.4)
        
        # Audio quality score
        if metrics.audio_quality > 0:
            scores.append(metrics.audio_quality)
        
        return statistics.mean(scores) if scores else 0.0
    
    def _get_time_to_join_status(self, time_to_join: Optional[int]) -> str:
        """Get status for time to join metric"""
        if not time_to_join:
            return 'unknown'
        if time_to_join <= self.thresholds['time_to_join_warning']:
            return 'good'
        elif time_to_join <= self.thresholds['time_to_join_error']:
            return 'warning'
        else:
            return 'error'
    
    def _get_uptime_status(self, uptime: float) -> str:
        """Get status for uptime metric"""
        if uptime >= self.thresholds['uptime_warning']:
            return 'good'
        elif uptime >= self.thresholds['uptime_error']:
            return 'warning'
        else:
            return 'error'
    
    def _get_stability_status(self, stability: float) -> str:
        """Get status for stability metric"""
        if stability >= self.thresholds['stability_warning']:
            return 'good'
        elif stability >= self.thresholds['stability_error']:
            return 'warning'
        else:
            return 'error'
    
    def _get_overall_status(self, score: float) -> str:
        """Get overall performance status"""
        if score >= 0.8:
            return 'excellent'
        elif score >= 0.6:
            return 'good'
        elif score >= 0.4:
            return 'fair'
        else:
            return 'poor'
