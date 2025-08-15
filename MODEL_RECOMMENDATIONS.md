# LMA Model Addition Recommendations

Based on testing results from `test_new_models.py`, here are the recommendations for adding new models to the LMA CloudFormation template.

## Test Results Summary

### ✅ **READY TO ADD (3 models)**
These models work with the current validation logic and Converse API:

| Model ID | Status | Performance Tier | Cost Tier |
|----------|--------|------------------|-----------|
| `amazon.nova-micro-v1:0` | ✅ Available | Entry-level | Lowest cost |
| `amazon.nova-lite-v1:0` | ✅ Available | Mid-tier | Low cost |
| `amazon.nova-pro-v1:0` | ✅ Available | High-tier | Medium cost |

### ⚠️ **REQUIRE INFERENCE PROFILES (7 models)**
These models need special handling but offer the most advanced capabilities:

| Model ID | Status | Performance Tier | Notes |
|----------|--------|------------------|-------|
| `amazon.nova-premier-v1:0` | ⚠️ Inference Profile | Highest | Most powerful Nova |
| `anthropic.claude-sonnet-4-20250514-v1:0` | ⚠️ Inference Profile | Highest | Claude 4 Sonnet |
| `anthropic.claude-opus-4-20250514-v1:0` | ⚠️ Inference Profile | Highest | Claude 4 Opus |
| `anthropic.claude-opus-4-1-20250805-v1:0` | ⚠️ Inference Profile | Highest | Claude 4 Opus v1.1 |
| `anthropic.claude-3-5-haiku-20241022-v1:0` | ⚠️ Inference Profile | High | Latest Haiku |
