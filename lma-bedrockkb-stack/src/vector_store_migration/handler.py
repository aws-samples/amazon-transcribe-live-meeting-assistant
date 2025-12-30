# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

import boto3
import cfnresponse
import json
import logging
import os
import time
from botocore.exceptions import ClientError

# Set up logging
logger = logging.getLogger()
log_level = os.environ.get('LOG_LEVEL', 'INFO')
logger.setLevel(getattr(logging, log_level))

def lambda_handler(event, context):
    """
    Custom Resource to handle migration between vector store types.
    Detects when switching from OpenSearch to S3 Vectors (or vice versa) and handles cleanup.
    """
    response_sent = False
    
    try:
        logger.info(f"Event: {json.dumps(event)}")
        
        request_type = event['RequestType']
        properties = event['ResourceProperties']
        
        current_vector_store = properties.get('CurrentVectorStore', 'OPENSEARCH_SERVERLESS')
        target_vector_store = properties.get('TargetVectorStore', 'OPENSEARCH_SERVERLESS')
        region = properties.get('Region', 'us-east-1')
        stack_name = properties.get('StackName', '')
        
        logger.info(f"Request: {request_type}, Current: {current_vector_store}, Target: {target_vector_store}")
        
        # Initialize clients
        bedrock_agent = boto3.client('bedrock-agent', region_name=region)
        aoss = boto3.client('opensearchserverless', region_name=region)
        s3vectors = boto3.client('s3vectors', region_name=region)
        
        response_data = {}
        
        if request_type in ['Create', 'Update']:
            # Check if we're switching vector store types
            if current_vector_store != target_vector_store:
                logger.info(f"Detected vector store migration: {current_vector_store} → {target_vector_store}")
                
                if current_vector_store == 'OPENSEARCH_SERVERLESS' and target_vector_store == 'S3_VECTORS':
                    # Migrating from OpenSearch to S3 Vectors
                    logger.info("Cleaning up OpenSearch resources...")
                    cleanup_opensearch_resources(bedrock_agent, aoss, stack_name)
                    response_data['MigrationPerformed'] = 'OpenSearch_to_S3Vectors'
                    
                elif current_vector_store == 'S3_VECTORS' and target_vector_store == 'OPENSEARCH_SERVERLESS':
                    # Migrating from S3 Vectors to OpenSearch
                    logger.info("Cleaning up S3 Vectors resources...")
                    cleanup_s3vectors_resources(bedrock_agent, s3vectors, stack_name)
                    response_data['MigrationPerformed'] = 'S3Vectors_to_OpenSearch'
                    
            else:
                logger.info("No vector store migration needed")
                response_data['MigrationPerformed'] = 'None'
                
        elif request_type == 'Delete':
            logger.info("Delete request - no action needed")
            response_data['Status'] = 'Deleted'
        
        # Send success response
        physical_id = event.get('PhysicalResourceId', f"migration-{stack_name}")
        cfnresponse.send(event, context, cfnresponse.SUCCESS, response_data, physicalResourceId=physical_id)
        response_sent = True
        
    except Exception as e:
        error_msg = f"Error in vector store migration: {str(e)}"
        logger.error(error_msg)
        logger.error(f"Exception type: {type(e).__name__}")
        
        physical_id = event.get('PhysicalResourceId', context.log_stream_name)
        cfnresponse.send(event, context, cfnresponse.FAILED, {}, 
                       physicalResourceId=physical_id, reason=error_msg[:1024])
        response_sent = True
    
    if not response_sent:
        logger.error("WARNING: No CloudFormation response was sent")
        
    return {"statusCode": 200}


def cleanup_opensearch_resources(bedrock_agent, aoss, stack_name):
    """
    Clean up OpenSearch Serverless resources when migrating to S3 Vectors.
    """
    try:
        # Find Knowledge Bases with OpenSearch storage
        logger.info("Searching for OpenSearch Knowledge Bases...")
        kb_list = bedrock_agent.list_knowledge_bases(maxResults=100)
        
        for kb_summary in kb_list.get('knowledgeBaseSummaries', []):
            kb_name = kb_summary.get('name', '')
            kb_id = kb_summary.get('knowledgeBaseId', '')
            
            # Check if this KB belongs to our stack
            if stack_name in kb_name and 'OpenSearch' in kb_name:
                logger.info(f"Found OpenSearch KB to delete: {kb_name} ({kb_id})")
                
                # Delete data sources first
                delete_all_data_sources(bedrock_agent, kb_id)
                
                # Delete the Knowledge Base
                try:
                    logger.info(f"Deleting Knowledge Base: {kb_id}")
                    bedrock_agent.delete_knowledge_base(knowledgeBaseId=kb_id)
                    logger.info(f"Successfully deleted KB: {kb_id}")
                    
                    # Wait for deletion to complete
                    wait_for_kb_deletion(bedrock_agent, kb_id)
                    
                except ClientError as e:
                    if e.response['Error']['Code'] != 'ResourceNotFoundException':
                        logger.warning(f"Error deleting KB {kb_id}: {e}")
        
        # Clean up OpenSearch collections
        logger.info("Cleaning up OpenSearch collections...")
        cleanup_opensearch_collections(aoss, stack_name)
        
    except Exception as e:
        logger.error(f"Error cleaning up OpenSearch resources: {e}")
        # Don't raise - allow migration to continue


def cleanup_s3vectors_resources(bedrock_agent, s3vectors, stack_name):
    """
    Clean up S3 Vectors resources when migrating to OpenSearch.
    """
    try:
        # Find Knowledge Bases with S3 Vectors storage
        logger.info("Searching for S3 Vectors Knowledge Bases...")
        kb_list = bedrock_agent.list_knowledge_bases(maxResults=100)
        
        for kb_summary in kb_list.get('knowledgeBaseSummaries', []):
            kb_name = kb_summary.get('name', '')
            kb_id = kb_summary.get('knowledgeBaseId', '')
            
            # Check if this KB belongs to our stack and uses S3 Vectors
            if stack_name in kb_name and 'S3Vectors' in kb_name:
                logger.info(f"Found S3 Vectors KB to delete: {kb_name} ({kb_id})")
                
                # Delete data sources first
                delete_all_data_sources(bedrock_agent, kb_id)
                
                # Delete the Knowledge Base
                try:
                    logger.info(f"Deleting Knowledge Base: {kb_id}")
                    bedrock_agent.delete_knowledge_base(knowledgeBaseId=kb_id)
                    logger.info(f"Successfully deleted KB: {kb_id}")
                    
                    # Wait for deletion to complete
                    wait_for_kb_deletion(bedrock_agent, kb_id)
                    
                except ClientError as e:
                    if e.response['Error']['Code'] != 'ResourceNotFoundException':
                        logger.warning(f"Error deleting KB {kb_id}: {e}")
        
        # Note: S3 Vectors buckets are managed by the S3VectorManager custom resource
        # and will be cleaned up by CloudFormation when those resources are removed
        
    except Exception as e:
        logger.error(f"Error cleaning up S3 Vectors resources: {e}")
        # Don't raise - allow migration to continue


def delete_all_data_sources(bedrock_agent, kb_id):
    """Delete all data sources for a Knowledge Base."""
    try:
        logger.info(f"Listing data sources for KB: {kb_id}")
        ds_list = bedrock_agent.list_data_sources(knowledgeBaseId=kb_id, maxResults=100)
        
        for ds in ds_list.get('dataSourceSummaries', []):
            ds_id = ds.get('dataSourceId')
            ds_name = ds.get('name', '')
            
            try:
                logger.info(f"Deleting data source: {ds_name} ({ds_id})")
                
                # First, update to RETAIN policy to avoid vector store deletion issues
                try:
                    bedrock_agent.update_data_source(
                        knowledgeBaseId=kb_id,
                        dataSourceId=ds_id,
                        name=ds_name,
                        dataDeletionPolicy='RETAIN'
                    )
                    logger.info(f"Updated data source {ds_id} to RETAIN policy")
                except Exception as update_error:
                    logger.warning(f"Could not update data source policy: {update_error}")
                
                # Now delete the data source
                bedrock_agent.delete_data_source(
                    knowledgeBaseId=kb_id,
                    dataSourceId=ds_id
                )
                logger.info(f"Successfully deleted data source: {ds_id}")
                
                # Wait a bit for deletion to process
                time.sleep(2)
                
            except ClientError as e:
                if e.response['Error']['Code'] != 'ResourceNotFoundException':
                    logger.warning(f"Error deleting data source {ds_id}: {e}")
                    
    except ClientError as e:
        if e.response['Error']['Code'] != 'ResourceNotFoundException':
            logger.warning(f"Error listing data sources for KB {kb_id}: {e}")


def wait_for_kb_deletion(bedrock_agent, kb_id, max_wait=300):
    """Wait for Knowledge Base to be fully deleted."""
    logger.info(f"Waiting for KB {kb_id} to be deleted...")
    elapsed = 0
    wait_interval = 10
    
    while elapsed < max_wait:
        try:
            bedrock_agent.get_knowledge_base(knowledgeBaseId=kb_id)
            # KB still exists, wait more
            logger.info(f"KB {kb_id} still exists, waiting... ({elapsed}s)")
            time.sleep(wait_interval)
            elapsed += wait_interval
        except ClientError as e:
            if e.response['Error']['Code'] == 'ResourceNotFoundException':
                logger.info(f"KB {kb_id} successfully deleted")
                return True
            else:
                logger.warning(f"Error checking KB status: {e}")
                time.sleep(wait_interval)
                elapsed += wait_interval
    
    logger.warning(f"Timeout waiting for KB {kb_id} deletion (waited {elapsed}s)")
    return False


def cleanup_opensearch_collections(aoss, stack_name):
    """Clean up OpenSearch Serverless collections."""
    try:
        # List collections
        collections = aoss.list_collections()
        
        for collection in collections.get('collectionSummaries', []):
            coll_name = collection.get('name', '')
            coll_id = collection.get('id', '')
            
            # Check if this collection belongs to our stack
            if stack_name.lower() in coll_name.lower():
                logger.info(f"Found OpenSearch collection to delete: {coll_name} ({coll_id})")
                
                try:
                    aoss.delete_collection(id=coll_id)
                    logger.info(f"Successfully deleted collection: {coll_id}")
                except ClientError as e:
                    logger.warning(f"Error deleting collection {coll_id}: {e}")
                    
    except Exception as e:
        logger.warning(f"Error cleaning up OpenSearch collections: {e}")