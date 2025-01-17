# Changelog

## Version 1.2.0 - Internal release
- Added support for Python 3.8, 3.9, 3.10 (experimental), 3.11 (experimental)
- Python 2.7 is now deprecated

## Version 1.1.1 - Bugfix release
- Add support of v1beta1 apiVersion when attaching

## Version 1.1.0 - Feature release
- Add support for fully-managed private clusters

## Version 1.0.9 - Bugfix release
- Throwing an exception when command invoked by `EksctlCommand.run_and_get_output` or `AwsCommand.run_and_get_output` fails
- Handle creating fully-private clusters (nodes in private subnets and private control plane endpoint)

## Version 1.0.8 - Feature and bugfix release
- Add option to install Metrics Server
- Fix "Inspect node pools" macro when using managed node groups
- Support tagging nodes
- Remove macro `Run Kubectl command` (natively supported in DSS 10.0.6)
- Support spot instances in node groups
- Update autoscaler to v1.20.2

## Version 1.0.7 - Bugfix release
- Add capability to assume IAM role on all cluster operation
- Fix use of `AWS_DEFAULT_REGION` environment variable

## Version 1.0.6 - Bugfix release
- Trim security group parameter string
- Update eksctl download URL
- Fix several Python 3 related issues

## Version 1.0.5 - Internal release
- Add support for Python 3

## Version 1.0.4 - Internal release
- Add GPU Driver support

## Version 1.0.3 - Bugfix release
- Fix the "resize cluster" macro

## Version 1.0.2 - Bugfix release
- Fix `Test network connectivity` macro when the hostname is already an IP.
