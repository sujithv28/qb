#!/usr/bin/env bash

set -e

python setup.py download

mkdir -p data/external/wikipedia
aws s3 cp s3://pinafore-us-west-2/public/wiki_redirects.csv data/external/wikipedia/all_wiki_redirects.csv

mkdir -p data/external/deep
aws s3 cp s3://pinafore-us-west-2/public/glove.6B.300d.txt data/external/deep/glove.6B.300d.txt
