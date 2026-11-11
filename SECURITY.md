# Security

self-service-platform is a local control-plane simulation. Create, update, and destroy run in that simulation. Amazon Bedrock and CloudWatch Logs are opt-in adapters and run only when their environment variables are set. This repository does not store credentials. When an adapter is enabled, credentials come from the normal AWS SDK chain.

If you find a security problem, report it through GitHub's private vulnerability reporting for this repository.
