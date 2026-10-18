{{/* Labels on every object of this chart. */}}
{{- define "datahub-kafka.labels" -}}
{{ include "datahub-kafka.selector" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}

{{/* The broker's pod; charts/datahub-network-policies selects it by name. */}}
{{- define "datahub-kafka.selector" -}}
app.kubernetes.io/name: datahub-kafka
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}
