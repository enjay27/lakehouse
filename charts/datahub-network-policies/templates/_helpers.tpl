{{/* Labels on every policy of this chart. */}}
{{- define "datahub-network-policies.labels" -}}
app.kubernetes.io/name: datahub-network-policies
app.kubernetes.io/instance: {{ .Release.Name }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}

{{/* Every CNPG instance in the namespace: datahub-pg's. */}}
{{- define "datahub-network-policies.instances" -}}
matchLabels:
  cnpg.io/podRole: instance
{{- end }}
