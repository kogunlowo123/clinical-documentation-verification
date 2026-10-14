variable "name" {
  description = "Resource name prefix"
  type        = string
  default     = "clinical-documentation-verification"
}

variable "region" {
  description = "AWS region"
  type        = string
}

variable "vpc_id" {
  description = "VPC hosting the EKS cluster"
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnets for the database (at least two AZs)"
  type        = list(string)
  validation {
    condition     = length(var.private_subnet_ids) >= 2
    error_message = "Provide subnets in at least two availability zones."
  }
}

variable "cluster_security_group_id" {
  description = "Security group of the EKS nodes allowed to reach PostgreSQL"
  type        = string
}

variable "eks_oidc_provider_arn" {
  description = "ARN of the EKS cluster IAM OIDC provider"
  type        = string
}

variable "eks_oidc_issuer_url" {
  description = "EKS cluster OIDC issuer URL"
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace of the verification service"
  type        = string
  default     = "clinical-documentation"
}

variable "service_account" {
  description = "Kubernetes service account of the verification service"
  type        = string
  default     = "clinical-documentation-verification"
}

variable "bedrock_model_arns" {
  description = "ARNs of the Bedrock foundation models / inference profiles approved for PHI extraction (BAA-covered)"
  type        = list(string)
  validation {
    condition     = length(var.bedrock_model_arns) >= 1 && alltrue([for arn in var.bedrock_model_arns : startswith(arn, "arn:aws:bedrock:")])
    error_message = "Provide at least one Bedrock model or inference-profile ARN."
  }
}

variable "db_instance_class" {
  description = "RDS instance class"
  type        = string
  default     = "db.r6g.large"
}

variable "db_allocated_storage_gb" {
  description = "Initial database storage"
  type        = number
  default     = 50
}

variable "audit_retention_days" {
  description = "Object Lock compliance retention for audit chains"
  type        = number
  default     = 2555 # 7 years
}

variable "tags" {
  description = "Tags applied to every resource"
  type        = map(string)
  default     = { "app.kubernetes.io/part-of" = "clinical-documentation-verification" }
}
