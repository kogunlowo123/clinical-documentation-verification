# Runbook: first deployment and startup

## Prerequisites

- Kubernetes ≥ 1.27 (EKS reference) with External Secrets Operator and a
  `ClusterSecretStore` named `platform-secrets` for the cloud secret manager.
- EHR: a SMART backend-services client registered for this service with
  system scopes for Patient, Condition, MedicationRequest, AllergyIntolerance
  (read) and DocumentReference, Provenance (write); its JWKS contains the
  public key generated in step 2.
- Enterprise IdP: app registration issuing clinician tokens with a roles claim
  (`physician`, `nurse-practitioner`, `physician-assistant`,
  `registered-nurse`, `medical-scribe`) and a SMART `fhirUser` claim.
- Agent identity fabric (optional but recommended): this service registered as
  an agent with scope `fhir:write:DocumentReference`; scribe agents registered
  with `documentation:verify` / `documentation:read`.
- Bedrock: model access granted for the primary model and Amazon Nova Pro in
  the region, under the organization's BAA; model invocation logging disabled
  or routed to PHI-approved storage.

## 1. Provision cloud resources

```bash
cd deployments/terraform/aws        # or azure / gcp
terraform init                      # configure an encrypted remote backend first
terraform apply -var-file=<environment>.tfvars
```

Record the outputs: service-account role ARN, database secret name, KMS key,
audit bucket.

## 2. Create keys outside Terraform

```bash
# SMART backend-services client key (ES384); publish the public JWK to the EHR.
openssl ecparam -name secp384r1 -genkey -noout | openssl pkcs8 -topk8 -nocrypt > fhir-client.pem
aws secretsmanager create-secret --name clinical-documentation-verification-fhir-client-key \
  --kms-key-id <kms_key_arn> --secret-string file://fhir-client.pem

# Ed25519 audit signing key.
openssl genpkey -algorithm ed25519 > audit-2026-10.pem
aws secretsmanager create-secret --name clinical-documentation-verification-audit-key-2026-10 \
  --kms-key-id <kms_key_arn> --secret-string file://audit-2026-10.pem
shred -u fhir-client.pem audit-2026-10.pem
```

Keep the audit public key (`openssl pkey -in audit-2026-10.pem -pubout`)
with the audit evidence before shredding.

## 3. Namespace and secret sync

```bash
kubectl apply -k deployments/kubernetes
kubectl -n clinical-documentation get externalsecret   # all READY=True
```

## 4. Install the chart

Create `<environment>.yaml` with every value marked REQUIRED in `values.yaml`
plus the IRSA annotation from Terraform:

```bash
helm upgrade --install cdv oci://ghcr.io/kogunlowo123/charts/clinical-documentation-verification \
  --version 0.1.0 -n clinical-documentation -f <environment>.yaml --wait
```

## 5. Verify

```bash
kubectl -n clinical-documentation rollout status statefulset/cdv-clinical-documentation-verification
kubectl -n clinical-documentation port-forward svc/cdv-clinical-documentation-verification 8080:80
curl -sf localhost:8080/readyz          # {"status":"ready","extraction":"model+rules"}
curl -s localhost:8080/.well-known/agent-card.json | jq .url
```

Then run one verification for a test patient in the EHR's non-production
environment and confirm the DocumentReference and Provenance appear after
approval. The Grafana dashboard shows traffic; Prometheus target
`clinical-documentation-verification` is up.
