# Scalable LLM Inference on Kubernetes with NVIDIA NIMS, LangChain, Milvus and FluxCD

This is a companion repository to the talk titled _Scalable LLM Inference on Kubernetes With NVIDIA NIMS, LangChain, Milvus and FluxCD_ scheduled at AI_dev on August 28, 2025.
Code is provided as reference for demo purposes. In a production environment, restrict privileges according to the principle of least privilege.

## Architecture

- **NIM Operator**: Manages NVIDIA NIMs deployment
- **NIM Services**: 
  - `meta-llama-3-2-1b-instruct` for chat completion
  - `nv-embedqa-e5-v5` for embeddings
- **NIM Caches**: Optimized model caching
- **Milvus**: Vector database for document storage
- **Karpenter**: GPU node provisioning
- **Gradio Client**: LangChain-based RAG interface

![architecture diagram](./images/architecture_diagram.png)

## Prerequisites

- AWS account
- NVIDIA NIM access
- **Fork this repository** (Flux will commit its files to it)
- it is assumed that commands are executed from Linux-based OS

## Deployment

1. **Setup environment variables**:

As per above, first you need to **fork this repository**, then setup the following environment variables (set `GITHUB_REPO` to the url of your forked repository):

```bash
export CLUSTER_NAME={your cluster name}
export AWS_DEFAULT_REGION={your region}
export NVIDIA_NGC_API_KEY={your NVIDIA api key}
export GITHUB_TOKEN={GitHub token}
export GITHUB_USER={GitHub user}
export GITHUB_REPO={your forked repo}
```

2. **Create an EFS file system**:

```bash
EFS_FS_ID=$(aws efs create-file-system \
    --region $AWS_DEFAULT_REGION \
    --performance-mode generalPurpose \
    --query 'FileSystemId' \
    --output text)
```

3. **Create the Amazon EKS cluster**:

```bash
cat << EOF | eksctl create cluster -f -
apiVersion: eksctl.io/v1alpha5
kind: ClusterConfig

metadata:
  name: ${CLUSTER_NAME}
  region: ${AWS_DEFAULT_REGION}

autoModeConfig:
  enabled: true

addons:
- name: aws-efs-csi-driver
  useDefaultPodIdentityAssociations: true
EOF
```

4. **Setup connectivity cluster-EFS**

```bash
VPC_ID=$(aws eks describe-cluster \
    --name $CLUSTER_NAME \
    --query "cluster.resourcesVpcConfig.vpcId" \
    --output text \
    --region=$AWS_DEFAULT_REGION)

CIDR_RANGE=$(aws ec2 describe-vpcs \
    --vpc-ids $VPC_ID \
    --query "Vpcs[].CidrBlock" \
    --output text \
    --region $AWS_DEFAULT_REGION)

SECURITY_GROUP_ID=$(aws ec2 create-security-group \
    --group-name ${CLUSTER_NAME}-EfsSecurityGroup \
    --description "${CLUSTER_NAME} EFS security group" \
    --vpc-id $VPC_ID \
    --region $AWS_DEFAULT_REGION \
    --query 'GroupId' \
    --output text)

aws ec2 authorize-security-group-ingress \
    --group-id $SECURITY_GROUP_ID \
    --protocol tcp \
    --port 2049 \
    --region $AWS_DEFAULT_REGION \
    --cidr $CIDR_RANGE

for subnet in $(aws eks describe-cluster \
    --name $CLUSTER_NAME \
    --query 'cluster.resourcesVpcConfig.subnetIds[]' \
    --region $AWS_DEFAULT_REGION \
    --output text); do
    aws efs create-mount-target \
        --file-system-id $EFS_FS_ID \
        --subnet-id $subnet \
        --security-groups $SECURITY_GROUP_ID \
        --region $AWS_DEFAULT_REGION 
done
```
5. **Create NVIDIA Secrets**:

```bash
kubectl create namespace nim-service

kubectl create secret -n nim-service docker-registry ngc-secret \
    --docker-server=nvcr.io \
    --docker-username='$oauthtoken' \
    --docker-password=$NVIDIA_NGC_API_KEY

kubectl create secret -n nim-service generic ngc-api-secret \
    --from-literal=NGC_API_KEY=$NVIDIA_NGC_API_KEY
```
6. **Create Flux namespace and ConfigMap to host EFS file system id**

```bash

kubectl create namespace flux-system

cat << EOF | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: cluster-config
  namespace: flux-system
data:
  EFS_FS_ID: ${EFS_FS_ID}
EOF
```

7. **Install and bootstrap Flux**

```bash
brew install fluxcd/tap/flux

flux bootstrap github \
--owner=${GITHUB_USER} \
--repository=${GITHUB_REPO} \
--branch=main \
--personal \
--path=llm-inference-nims-langchain-milvus-fluxcd/cluster

```

8. **Monitor deployment progress and wait for completion**

```bash
flux get kustomizations --watch
```

9. **Launch the client app**

```bash
kubectl port-forward service/meta-llama-3-2-1b-instruct -n nim-service 8000:8000 & \
kubectl port-forward service/nv-embedqa-e5-v5 -n nim-service 8001:8000 & \
kubectl port-forward service/milvus -n vectorstore 8002:19530 & \
kubectl port-forward service/milvus -n vectorstore 8003:9091 &

cd ./llm-inference-nims-langchain-milvus-fluxcd/client
python3 -m venv env
source env/bin/activate
python3 -m pip install --quiet -r requirements.txt
python3 gradio_app.py
```

## Usage

Open a browser window at http://127.0.0.1:7860 to interact with the RAG chat assistant.

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for more information.

## License

This library is licensed under the MIT-0 License. See the LICENSE file.