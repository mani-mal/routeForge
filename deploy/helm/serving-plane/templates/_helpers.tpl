{{/*
Chart name, truncated/sanitized to a DNS-1123 safe value.
*/}}
{{- define "serving-plane.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Fully qualified app name, honoring fullnameOverride/nameOverride.
*/}}
{{- define "serving-plane.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "serving-plane.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Common labels.
*/}}
{{- define "serving-plane.labels" -}}
helm.sh/chart: {{ include "serving-plane.chart" . }}
{{ include "serving-plane.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/*
Base selector labels, shared by both components.
*/}}
{{- define "serving-plane.selectorLabels" -}}
app.kubernetes.io/name: {{ include "serving-plane.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/*
Component-scoped names/labels — pass (dict "root" . "component" "api"|"ui").
*/}}
{{- define "serving-plane.componentFullname" -}}
{{- printf "%s-%s" (include "serving-plane.fullname" .root) .component | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "serving-plane.componentLabels" -}}
{{ include "serving-plane.labels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "serving-plane.componentSelectorLabels" -}}
{{ include "serving-plane.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Name of the Secret holding Langfuse keys — either the user-supplied existing
Secret, or the chart-created one (dev only). Never a values-embedded literal.
*/}}
{{- define "serving-plane.langfuseSecretName" -}}
{{- if .Values.langfuse.existingSecret -}}
{{- .Values.langfuse.existingSecret -}}
{{- else -}}
{{- printf "%s-langfuse" (include "serving-plane.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
api image ref.
*/}}
{{- define "serving-plane.apiImage" -}}
{{- printf "%s:%s" .Values.image.api.repository (.Values.image.api.tag | default .Chart.AppVersion) -}}
{{- end -}}

{{/*
ui image ref.
*/}}
{{- define "serving-plane.uiImage" -}}
{{- printf "%s:%s" .Values.image.ui.repository (.Values.image.ui.tag | default .Chart.AppVersion) -}}
{{- end -}}
