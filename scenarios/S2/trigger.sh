#!/usr/bin/env bash
# S2 - large-file IO starvation: run the nightly archiver NOW.
kubectl create job --from=cronjob/log-archiver s2-run-$(date +%s) -n factory-data
echo "S2 fired - tar+gzip of shared PVC; distinguishable from S1 (sequential read-heavy, different root)"
