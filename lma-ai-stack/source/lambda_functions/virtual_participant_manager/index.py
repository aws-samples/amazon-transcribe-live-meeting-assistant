"""
Virtual Participant Manager Lambda Function
Handles enhanced status tracking, metrics, and error reporting for Virtual Participants
"""

import json
import boto3
import logging
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict
from enum import Enum

from error_analyzer import ErrorAnalyzer
from performance_monitor import PerformanceMonitor

# Configure logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients
dynamodb = boto3.resource('dynamodb')
appsync = boto3.client('appsync')

class VPStatus(Enum):
    INITIALIZING = "INITIALIZING"
    CONNECTING = "CONNECTING"
    JOINING = "JOINING"
    JOINED = "JOINED"
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ENDED = "ENDED"

class ErrorCategory(Enum):
    AUTHENTICATION_ERROR = "AUTHENTICATION_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"
    MEETING_NOT_FOUND = "MEETING_NOT_FOUND"
    MEETING_ENDED = "MEETING_ENDED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    INVALID_CREDENTIALS = "INVALID_CREDENTIALS"
    PLATFORM_ERROR = "PLATFORM_ERROR"
    TIMEOUT_ERROR = "TIMEOUT_ERROR"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"

@dataclass
class StatusHistoryEntry:
    status: str
    timestamp: str
    message: Optional[str] = None
    error_details: Optional[str] = None
    duration: Optional[int] = None
    metadata: Optional[str] = None

@dataclass
class ConnectionDetails:
    join_attempts: int = 0
    successful_joins: int = 0
    last_join_attempt: Optional[str] = None
    connection_duration: int = 0
    disconnection_reason: Optional[str] = None
    network_latency: Optional[float] = None
    audio_quality: Optional[float] = None
    connection_stability: Optional[float] = None

@dataclass
class ErrorDetails:
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    error_category: Optional[str] = None
    troubleshooting_steps: Optional[List[str]] = None
    last_error_at: Optional[str] = None
    error_count: int = 0

@dataclass
class Metrics:
    total_duration: int = 0
    time_to_join: Optional[int] = None
    uptime: float = 0.0
    average_latency: Optional[float] = None
    transcript_segments: int = 0
    audio_minutes: float = 0.0
    last_activity: Optional[str] = None

class VirtualParticipantManager:
    def __init__(self, table_name: str):
        self.table = dynamodb.Table(table_name)
        self.error_analyzer = ErrorAnalyzer()
        self.performance_monitor = PerformanceMonitor()
        
    def get_current_timestamp(self) -> str:
        """Get current timestamp in ISO format"""
        return datetime.now(timezone.utc).isoformat()
    
    def calculate_duration(self, start_time: str, end_time: str = None) -> int:
        """Calculate duration between two timestamps in milliseconds"""
        start = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
        end = datetime.fromisoformat((end_time or self.get_current_timestamp()).replace('Z', '+00:00'))
        return int((end - start).total_seconds() * 1000)
    
    def get_virtual_participant(self, vp_id: str) -> Optional[Dict[str, Any]]:
        """Get Virtual Participant by ID"""
        try:
            response = self.table.get_item(Key={'id': vp_id})
            return response.get('Item')
        except Exception as e:
            logger.error(f"Error getting VP {vp_id}: {str(e)}")
            return None
    
    def update_virtual_participant_status(self, vp_id: str, status: str, 
                                        message: str = None, 
                                        error_details: Dict = None,
                                        connection_details: Dict = None,
                                        metrics: Dict = None,
                                        metadata: str = None) -> Dict[str, Any]:
        """Update Virtual Participant status with enhanced tracking"""
        
        current_time = self.get_current_timestamp()
        vp = self.get_virtual_participant(vp_id)
        
        if not vp:
            raise ValueError(f"Virtual Participant {vp_id} not found")
        
        # Calculate duration in previous status
        previous_status_duration = None
        if vp.get('updatedAt'):
            previous_status_duration = self.calculate_duration(vp['updatedAt'], current_time)
        
        # Create status history entry
        status_entry = StatusHistoryEntry(
            status=status,
            timestamp=current_time,
            message=message,
            error_details=json.dumps(error_details) if error_details else None,
            duration=previous_status_duration,
            metadata=metadata
        )
        
        # Get existing status history
        status_history = vp.get('statusHistory', [])
        status_history.append(asdict(status_entry))
        
        # Update connection details
        existing_connection = vp.get('connectionDetails', {})
        if connection_details:
            existing_connection.update(connection_details)
        
        # Update error details if status is FAILED
        existing_error_details = vp.get('errorDetails', {})
        if status == VPStatus.FAILED.value and error_details:
            # Use error analyzer for intelligent error categorization
            error_report = self.error_analyzer.create_error_report(
                error_message=error_details.get('errorMessage', ''),
                error_code=error_details.get('errorCode'),
                platform=vp.get('meetingPlatform', '').lower(),
                context={'vpId': vp_id, 'meetingId': vp.get('meetingId')}
            )
            
            existing_error_details.update({
                'errorCode': error_details.get('errorCode'),
                'errorMessage': error_details.get('errorMessage'),
                'errorCategory': error_report['errorCategory'],
                'troubleshootingSteps': error_report['troubleshootingSteps'],
                'severity': error_report['severity'],
                'isRetryable': error_report['isRetryable'],
                'lastErrorAt': current_time,
                'errorCount': existing_error_details.get('errorCount', 0) + 1
            })
        
        # Update metrics
        existing_metrics = vp.get('metrics', {})
        if metrics:
            existing_metrics.update(metrics)
        
        # Calculate total duration if completed/ended
        if status in [VPStatus.COMPLETED.value, VPStatus.ENDED.value, VPStatus.FAILED.value]:
            total_duration = self.calculate_duration(vp['createdAt'], current_time)
            existing_metrics['totalDuration'] = total_duration
            
            # Calculate time to join if we have JOINED status in history
            for entry in status_history:
                if entry['status'] == VPStatus.JOINED.value:
                    time_to_join = self.calculate_duration(vp['createdAt'], entry['timestamp'])
                    existing_metrics['timeToJoin'] = time_to_join
                    break
        
        existing_metrics['lastActivity'] = current_time
        
        # Update the item in DynamoDB
        update_expression = """
            SET #status = :status,
                updatedAt = :updated_at,
                statusHistory = :status_history,
                connectionDetails = :connection_details,
                errorDetails = :error_details,
                metrics = :metrics
        """
        
        expression_attribute_names = {
            '#status': 'status'
        }
        
        expression_attribute_values = {
            ':status': status,
            ':updated_at': current_time,
            ':status_history': status_history,
            ':connection_details': existing_connection,
            ':error_details': existing_error_details,
            ':metrics': existing_metrics
        }
        
        # Add endedAt if status is terminal
        if status in [VPStatus.COMPLETED.value, VPStatus.ENDED.value, VPStatus.FAILED.value]:
            update_expression += ", endedAt = :ended_at"
            expression_attribute_values[':ended_at'] = current_time
        
        try:
            response = self.table.update_item(
                Key={'id': vp_id},
                UpdateExpression=update_expression,
                ExpressionAttributeNames=expression_attribute_names,
                ExpressionAttributeValues=expression_attribute_values,
                ReturnValues='ALL_NEW'
            )
            
            updated_vp = response['Attributes']
            
            # Generate performance analysis if VP is completed
            if status in [VPStatus.COMPLETED.value, VPStatus.ENDED.value]:
                performance_analysis = self.performance_monitor.analyze_performance(updated_vp)
                logger.info(f"Performance analysis for VP {vp_id}: {performance_analysis}")
            
            logger.info(f"Updated VP {vp_id} status to {status}")
            return updated_vp
            
        except Exception as e:
            logger.error(f"Error updating VP {vp_id}: {str(e)}")
            raise
    
    def end_virtual_participant(self, vp_id: str, end_reason: str, ended_by: str = None) -> Dict[str, Any]:
        """End a Virtual Participant with performance analysis"""
        
        current_time = self.get_current_timestamp()
        
        # First update status with history
        updated_vp = self.update_virtual_participant_status(
            vp_id, 
            VPStatus.ENDED.value, 
            f"Virtual Participant ended: {end_reason}",
            metadata=json.dumps({'endedBy': ended_by, 'endReason': end_reason})
        )
        
        # Then update end-specific fields
        update_expression = """
            SET endedAt = :ended_at,
                endReason = :end_reason
        """
        
        expression_attribute_values = {
            ':ended_at': current_time,
            ':end_reason': end_reason
        }
        
        if ended_by:
            update_expression += ", endedBy = :ended_by"
            expression_attribute_values[':ended_by'] = ended_by
        
        try:
            response = self.table.update_item(
                Key={'id': vp_id},
                UpdateExpression=update_expression,
                ExpressionAttributeValues=expression_attribute_values,
                ReturnValues='ALL_NEW'
            )
            
            # Generate final performance analysis
            final_vp = response['Attributes']
            performance_analysis = self.performance_monitor.analyze_performance(final_vp)
            
            logger.info(f"Ended VP {vp_id}: {end_reason}")
            logger.info(f"Final performance analysis: {performance_analysis}")
            
            return final_vp
            
        except Exception as e:
            logger.error(f"Error ending VP {vp_id}: {str(e)}")
            raise
    
    def get_performance_analysis(self, vp_id: str) -> Dict[str, Any]:
        """Get performance analysis for a Virtual Participant"""
        
        vp = self.get_virtual_participant(vp_id)
        if not vp:
            raise ValueError(f"Virtual Participant {vp_id} not found")
        
        return self.performance_monitor.analyze_performance(vp)
    
    def link_to_meeting_transcript(self, vp_id: str, call_id: str) -> Dict[str, Any]:
        """Link Virtual Participant to a meeting transcript"""
        
        current_time = self.get_current_timestamp()
        
        try:
            response = self.table.update_item(
                Key={'id': vp_id},
                UpdateExpression="SET relatedCallId = :call_id, updatedAt = :updated_at",
                ExpressionAttributeValues={
                    ':call_id': call_id,
                    ':updated_at': current_time
                },
                ReturnValues='ALL_NEW'
            )
            
            logger.info(f"Linked VP {vp_id} to meeting {call_id}")
            return response['Attributes']
            
        except Exception as e:
            logger.error(f"Error linking VP {vp_id} to meeting {call_id}: {str(e)}")
            raise

def lambda_handler(event, context):
    """
    Lambda handler for Virtual Participant management operations
    """
    
    logger.info(f"Received event: {json.dumps(event)}")
    
    # Get table name from environment
    table_name = context.get('TABLE_NAME', 'VirtualParticipants')
    vp_manager = VirtualParticipantManager(table_name)
    
    try:
        # Parse the event
        operation = event.get('operation')
        arguments = event.get('arguments', {})
        
        if operation == 'updateVirtualParticipantStatus':
            input_data = arguments.get('input', {})
            result = vp_manager.update_virtual_participant_status(
                vp_id=input_data['id'],
                status=input_data['status'],
                message=input_data.get('message'),
                error_details=input_data.get('errorDetails'),
                connection_details=input_data.get('connectionDetails'),
                metrics=input_data.get('metrics'),
                metadata=input_data.get('metadata')
            )
            
        elif operation == 'endVirtualParticipant':
            input_data = arguments.get('input', {})
            result = vp_manager.end_virtual_participant(
                vp_id=input_data['id'],
                end_reason=input_data['endReason'],
                ended_by=input_data.get('endedBy')
            )
            
        elif operation == 'getVirtualParticipantEnhanced':
            vp_id = arguments.get('id')
            result = vp_manager.get_virtual_participant(vp_id)
            if not result:
                raise ValueError(f"Virtual Participant {vp_id} not found")
        
        elif operation == 'getVirtualParticipantMetrics':
            vp_id = arguments.get('id')
            result = vp_manager.get_performance_analysis(vp_id)
            
        elif operation == 'linkToMeetingTranscript':
            input_data = arguments.get('input', {})
            result = vp_manager.link_to_meeting_transcript(
                vp_id=input_data['vpId'],
                call_id=input_data['callId']
            )
                
        else:
            raise ValueError(f"Unknown operation: {operation}")
        
        return {
            'statusCode': 200,
            'body': result
        }
        
    except Exception as e:
        logger.error(f"Error processing request: {str(e)}")
        return {
            'statusCode': 500,
            'body': {
                'error': str(e)
            }
        }
