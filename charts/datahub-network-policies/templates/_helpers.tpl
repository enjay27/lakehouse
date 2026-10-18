{{/* Labels on every policy of this chart. */}}
{{- define "datahub-network-policies.labels" -}}
app.kubernetes.io/name: datahub-network-policies
app.kubernetes.io/instance: {{ .Release.Name }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}

{{/* A namespace selector from a {key, value} label. */}}
{{- define "datahub-network-policies.namespace" -}}
- namespaceSelector:
    matchLabels:
      {{ .key }}: {{ .value | quote }}
{{- end }}

{{/* Every CNPG instance in the namespace: datahub-pg's. */}}
{{- define "datahub-network-policies.instances" -}}
matchLabels:
  cnpg.io/podRole: instance
{{- end }}
