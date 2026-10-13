{{/* Labels on every policy of this chart. */}}
{{- define "polaris-network-policies.labels" -}}
app.kubernetes.io/name: polaris-network-policies
app.kubernetes.io/instance: {{ .Release.Name }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}

{{/* A namespace selector from a {key, value} label. */}}
{{- define "polaris-network-policies.namespace" -}}
- namespaceSelector:
    matchLabels:
      {{ .key }}: {{ .value | quote }}
{{- end }}

{{/* A namespace selector by name: Kubernetes sets kubernetes.io/metadata.name on every one. */}}
{{- define "polaris-network-policies.namespaceNamed" -}}
- namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: {{ . | quote }}
{{- end }}

{{/* Polaris's pods, by the labels upstream's chart sets with release name polaris
(install/release.py POLARIS). */}}
{{- define "polaris-network-policies.polaris" -}}
matchLabels:
  app.kubernetes.io/name: polaris
  app.kubernetes.io/instance: polaris
{{- end }}

{{/* Every CNPG instance in the namespace: polaris-pg's, and on the gate polaris-pg-restore's. */}}
{{- define "polaris-network-policies.instances" -}}
matchLabels:
  cnpg.io/podRole: instance
{{- end }}
