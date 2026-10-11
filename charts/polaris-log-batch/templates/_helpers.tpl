{{- define "polaris-log-batch.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: log-batch
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
