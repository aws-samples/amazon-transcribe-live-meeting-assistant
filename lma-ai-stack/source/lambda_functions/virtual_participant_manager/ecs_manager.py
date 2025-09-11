"""
ECS Task Manager for Virtual Participants
Handles actual termination of running ECS containers
"""

import boto3
import logging
from typing import Dict, List, Optional
import os

logger = logging.getLogger(__name__)

class ECSTaskManager:
    """
    Manages ECS tasks for Virtual Participants
    """
    
    def __init__(self):
        self.ecs_client = boto3.client('ecs')
        
        # Debug logging for environment variables
        logger.info(f"ECS_CLUSTER_NAME from env: {os.environ.get('ECS_CLUSTER_NAME', 'NOT_SET')}")
        logger.info(f"CLUSTER_NAME from env: {os.environ.get('CLUSTER_NAME', 'NOT_SET')}")
        logger.info(f"AWS_LAMBDA_FUNCTION_NAME: {os.environ.get('AWS_LAMBDA_FUNCTION_NAME', 'NOT_SET')}")
        logger.info(f"TABLE_NAME: {os.environ.get('TABLE_NAME', 'NOT_SET')}")
        
        # Try to get cluster name from environment, fallback to dynamic discovery
        self.cluster_name = (
            os.environ.get('ECS_CLUSTER_NAME') or 
            os.environ.get('CLUSTER_NAME') or
            self.find_vp_cluster()
        )
        logger.info(f"ECSTaskManager initialized with cluster: {self.cluster_name}")
    
    def get_stack_name_from_lambda(self) -> str:
        """Extract base stack name from Lambda function name or environment"""
        try:
            # Try to get from Lambda context first
            lambda_function_name = os.environ.get('AWS_LAMBDA_FUNCTION_NAME', '')
            logger.info(f"Lambda function name: {lambda_function_name}")
            
            if lambda_function_name:
                # Pattern: {base-stack-name}-AISTACK-{id}-VirtualParticipantManager
                # Example: end-button-AISTACK-58LCOGDXUZTI-VirtualParticipantManager
                # We want just the base part: end-button
                
                if '-VirtualParticipantManager' in lambda_function_name:
                    # Remove the suffix first
                    without_suffix = lambda_function_name.replace('-VirtualParticipantManager', '')
                    logger.info(f"Without suffix: {without_suffix}")
                    
                    # Look for AISTACK pattern and extract base stack name
                    if '-AISTACK-' in without_suffix:
                        base_stack_name = without_suffix.split('-AISTACK-')[0]
                        logger.info(f"Extracted base stack name from Lambda function: {base_stack_name}")
                        return base_stack_name
                    else:
                        # If no AISTACK pattern, use the whole thing
                        logger.info(f"No AISTACK pattern, using full name: {without_suffix}")
                        return without_suffix
            
            # Fallback: try to get from DynamoDB table name
            table_name = os.environ.get('TABLE_NAME', '')
            logger.info(f"Table name: {table_name}")
            
            if table_name and '-VirtualParticipant' in table_name:
                # Pattern: {base-stack-name}-AISTACK-{id}-VirtualParticipant
                without_suffix = table_name.replace('-VirtualParticipant', '')
                
                if '-AISTACK-' in without_suffix:
                    base_stack_name = without_suffix.split('-AISTACK-')[0]
                    logger.info(f"Extracted base stack name from table name: {base_stack_name}")
                    return base_stack_name
                else:
                    logger.info(f"Extracted stack name from table name: {without_suffix}")
                    return without_suffix
            
            logger.warning("Could not extract stack name from Lambda or table name")
            return None
            
        except Exception as e:
            logger.error(f"Error extracting stack name: {e}")
            return None
    
    def find_vp_cluster(self) -> str:
        """Find the Virtual Participant cluster dynamically using stack name pattern"""
        try:
            # Get stack name from Lambda function name
            stack_name = self.get_stack_name_from_lambda()
            logger.info(f"Using stack name for cluster search: {stack_name}")
            
            # List all clusters
            response = self.ecs_client.list_clusters()
            clusters = response.get('clusterArns', [])
            
            logger.info(f"Found {len(clusters)} ECS clusters")
            
            # Pattern: {stack-name}-VIRTUALPARTICIPANTSTACK-{id}-Cluster-{someid}
            if stack_name:
                for cluster_arn in clusters:
                    cluster_name = cluster_arn.split('/')[-1]  # Extract name from ARN
                    logger.info(f"Checking cluster: {cluster_name}")
                    
                    # Check if cluster matches the expected pattern
                    if (cluster_name.startswith(stack_name) and 
                        'VIRTUALPARTICIPANTSTACK' in cluster_name.upper()):
                        logger.info(f"Found matching VP cluster: {cluster_name}")
                        return cluster_name
            
            # If no specific VP cluster found, try the first cluster
            if clusters:
                first_cluster = clusters[0].split('/')[-1]
                logger.warning(f"No VP-specific cluster found, using first cluster: {first_cluster}")
                return first_cluster
            
            logger.error("No ECS clusters found")
            return 'default'
            
        except Exception as e:
            logger.error(f"Error finding VP cluster: {e}")
            return 'default'
        
    def find_vp_task(self, vp_id: str) -> Optional[str]:
        """
        Find the running ECS task for a Virtual Participant
        
        Args:
            vp_id: Virtual Participant ID
            
        Returns:
            Task ARN if found, None otherwise
        """
        try:
            logger.info(f"Searching for ECS task with VP ID: {vp_id} in cluster: {self.cluster_name}")
            
            # List all running tasks in the cluster
            response = self.ecs_client.list_tasks(
                cluster=self.cluster_name,
                desiredStatus='RUNNING'
            )
            
            if not response.get('taskArns'):
                logger.info(f"No running tasks found in cluster {self.cluster_name}")
                return None
            
            logger.info(f"Found {len(response['taskArns'])} running tasks in cluster")
            
            # Describe tasks to get details
            task_details = self.ecs_client.describe_tasks(
                cluster=self.cluster_name,
                tasks=response['taskArns']
            )
            
            # Find task with matching VP ID in environment variables or tags
            for task in task_details.get('tasks', []):
                task_arn = task['taskArn']
                task_definition = task.get('taskDefinitionArn', '')
                
                logger.info(f"Checking task: {task_arn}")
                logger.info(f"Task definition: {task_definition}")
                
                # Check if task definition contains VP-related keywords
                if any(keyword in task_definition.upper() for keyword in ['VIRTUALPARTICIPANT', 'VP', 'VIRTUAL-PARTICIPANT']):
                    logger.info(f"Found VP-related task definition: {task_definition}")
                
                # Get task definition details to check environment variables
                try:
                    task_def_response = self.ecs_client.describe_task_definition(
                        taskDefinition=task_definition
                    )
                    
                    containers = task_def_response.get('taskDefinition', {}).get('containerDefinitions', [])
                    
                    for container in containers:
                        env_vars = container.get('environment', [])
                        logger.info(f"Container {container.get('name', 'unknown')} has {len(env_vars)} environment variables")
                        
                        # Look for various VP ID environment variables
                        for env_var in env_vars:
                            env_name = env_var.get('name', '')
                            env_value = env_var.get('value', '')
                            
                            # Check multiple possible environment variable names
                            if env_name in ['VIRTUAL_PARTICIPANT_ID', 'VP_ID', 'PARTICIPANT_ID'] and env_value == vp_id:
                                logger.info(f"Found ECS task for VP {vp_id}: {task_arn} (matched via {env_name})")
                                return task_arn
                            
                            # Log all environment variables for debugging
                            if 'VP' in env_name.upper() or 'PARTICIPANT' in env_name.upper():
                                logger.info(f"  Found VP-related env var: {env_name}={env_value}")
                                
                except Exception as task_def_error:
                    logger.warning(f"Could not describe task definition {task_definition}: {task_def_error}")
                    continue
            
            logger.warning(f"No ECS task found for VP {vp_id} after checking all running tasks")
            return None
            
        except Exception as e:
            logger.error(f"Error finding ECS task for VP {vp_id}: {str(e)}")
            return None
    
    def stop_vp_task(self, vp_id: str, reason: str = "User requested termination") -> bool:
        """
        Stop the ECS task for a Virtual Participant
        
        Args:
            vp_id: Virtual Participant ID
            reason: Reason for stopping the task
            
        Returns:
            True if task was stopped successfully, False otherwise
        """
        try:
            logger.info(f"=== ATTEMPTING TO STOP ECS TASK FOR VP {vp_id} ===")
            logger.info(f"Cluster name: {self.cluster_name}")
            logger.info(f"Reason: {reason}")
            
            # Find the running task
            task_arn = self.find_vp_task(vp_id)
            
            if not task_arn:
                logger.warning(f"No running ECS task found for VP {vp_id}")
                logger.info("=== ECS TASK STOP FAILED - NO TASK FOUND ===")
                return False
            
            # Stop the task
            response = self.ecs_client.stop_task(
                cluster=self.cluster_name,
                task=task_arn,
                reason=reason
            )
            
            stopped_task = response.get('task', {})
            logger.info(f"Successfully stopped ECS task for VP {vp_id}: {task_arn}")
            logger.info(f"Task status: {stopped_task.get('lastStatus')}")
            
            return True
            
        except Exception as e:
            logger.error(f"Error stopping ECS task for VP {vp_id}: {str(e)}")
            return False
    
    def get_task_status(self, vp_id: str) -> Dict:
        """
        Get the current status of the ECS task for a Virtual Participant
        
        Args:
            vp_id: Virtual Participant ID
            
        Returns:
            Dictionary with task status information
        """
        try:
            task_arn = self.find_vp_task(vp_id)
            
            if not task_arn:
                return {
                    'found': False,
                    'status': 'NOT_FOUND',
                    'message': 'No running ECS task found'
                }
            
            # Get task details
            response = self.ecs_client.describe_tasks(
                cluster=self.cluster_name,
                tasks=[task_arn]
            )
            
            if response.get('tasks'):
                task = response['tasks'][0]
                return {
                    'found': True,
                    'status': task.get('lastStatus'),
                    'desired_status': task.get('desiredStatus'),
                    'health_status': task.get('healthStatus'),
                    'created_at': task.get('createdAt'),
                    'started_at': task.get('startedAt'),
                    'stopped_at': task.get('stoppedAt'),
                    'stop_reason': task.get('stoppedReason'),
                    'task_arn': task_arn
                }
            
            return {
                'found': False,
                'status': 'UNKNOWN',
                'message': 'Task details not available'
            }
            
        except Exception as e:
            logger.error(f"Error getting task status for VP {vp_id}: {str(e)}")
            return {
                'found': False,
                'status': 'ERROR',
                'message': str(e)
            }
    
    def list_all_vp_tasks(self) -> List[Dict]:
        """
        List all running Virtual Participant tasks
        
        Returns:
            List of task information dictionaries
        """
        try:
            # List all running tasks
            response = self.ecs_client.list_tasks(
                cluster=self.cluster_name,
                desiredStatus='RUNNING'
            )
            
            if not response.get('taskArns'):
                return []
            
            # Get task details
            task_details = self.ecs_client.describe_tasks(
                cluster=self.cluster_name,
                tasks=response['taskArns']
            )
            
            vp_tasks = []
            
            for task in task_details.get('tasks', []):
                # Extract VP ID from task if possible
                task_info = {
                    'task_arn': task['taskArn'],
                    'status': task.get('lastStatus'),
                    'desired_status': task.get('desiredStatus'),
                    'created_at': task.get('createdAt'),
                    'started_at': task.get('startedAt'),
                    'vp_id': None  # Will be populated if found
                }
                
                # Try to extract VP ID from task definition
                task_definition = task.get('taskDefinitionArn', '')
                if 'VirtualParticipant' in task_definition:
                    # This is likely a VP task
                    vp_tasks.append(task_info)
            
            return vp_tasks
            
        except Exception as e:
            logger.error(f"Error listing VP tasks: {str(e)}")
            return []
