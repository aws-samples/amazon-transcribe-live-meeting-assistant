
VERSION_FILE ?= ../VERSION
AWS_REGION ?= us-east-1
RELEASE_S3_BUCKET := $(RELEASE_S3_BUCKET_BASE)-$(AWS_REGION)
RELEASE_S3_PREFIX ?= release
RELEASE_VERSION ?= $(shell cat $(VERSION_FILE))

release:
	echo '[INFO] No ml-stacks templates to upload'

.PHONY: release
