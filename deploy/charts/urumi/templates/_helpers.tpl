{{- define "urumi.storeHost" -}}{{ default (printf "store.%s" .Values.domain) .Values.hosts.store }}{{- end -}}
{{- define "urumi.appHost" -}}{{ default (printf "app.%s" .Values.domain) .Values.hosts.app }}{{- end -}}
{{- define "urumi.storeUrl" -}}{{ .Values.scheme }}://{{ include "urumi.storeHost" . }}{{- end -}}

{{/* labels: include "urumi.labels" (list . "component") */}}
{{- define "urumi.labels" -}}
{{- $ctx := index . 0 -}}{{- $c := index . 1 -}}
app.kubernetes.io/name: {{ $c }}
app.kubernetes.io/component: {{ $c }}
app.kubernetes.io/part-of: urumi
app.kubernetes.io/instance: {{ $ctx.Release.Name }}
app.kubernetes.io/managed-by: {{ $ctx.Release.Service }}
{{- end -}}
{{- define "urumi.selector" -}}
app.kubernetes.io/component: {{ . }}
app.kubernetes.io/part-of: urumi
{{- end -}}

{{/* random value that survives `helm upgrade`: include "urumi.rand" (list $existingSecret "key" 24) */}}
{{- define "urumi.rand" -}}
{{- $ex := index . 0 -}}{{- $k := index . 1 -}}{{- $n := index . 2 -}}
{{- $v := "" -}}
{{- if $ex -}}{{- if hasKey $ex "data" -}}{{- if hasKey $ex.data $k -}}{{- $v = (index $ex.data $k | b64dec) -}}{{- end -}}{{- end -}}{{- end -}}
{{- if $v -}}{{ $v }}{{- else -}}{{ randAlphaNum $n }}{{- end -}}
{{- end -}}

{{- define "urumi.secretRef" -}}
secretKeyRef:
  name: {{ .ctx.Values.secrets.name }}
  key: {{ .key }}
{{- end -}}

{{/* pod-level and container-level hardening used by every workload we own */}}
{{- define "urumi.podSecurity" -}}
automountServiceAccountToken: false
securityContext:
  seccompProfile:
    type: RuntimeDefault
{{- end -}}
{{- define "urumi.containerSecurity" -}}
securityContext:
  allowPrivilegeEscalation: false
  readOnlyRootFilesystem: true
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  capabilities:
    drop: ["ALL"]
{{- end -}}

{{- define "urumi.pvcStorageClass" -}}
{{- if . }}
storageClassName: {{ . | quote }}
{{- end }}
{{- end -}}

{{- define "urumi.dnsEgress" }}
- to:
    - namespaceSelector:
        matchLabels: { kubernetes.io/metadata.name: kube-system }
      podSelector:
        matchLabels: { k8s-app: kube-dns }
  ports:
    - { protocol: UDP, port: 53 }
    - { protocol: TCP, port: 53 }
{{- end }}
{{- define "urumi.internetEgress" }}
# Public internet only: RFC1918 (incl. pod/service CIDRs), link-local and cloud-metadata are excluded.
- to:
    - ipBlock:
        cidr: 0.0.0.0/0
        except: ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"]
  ports:
    - { protocol: TCP, port: 80 }
    - { protocol: TCP, port: 443 }
{{- end }}
{{- define "urumi.wpDbEnv" }}
- { name: WORDPRESS_DB_HOST, value: "mariadb:3306" }
- { name: WORDPRESS_DB_NAME, value: wordpress }
- { name: WORDPRESS_DB_USER, value: wordpress }
- name: WORDPRESS_DB_PASSWORD
  valueFrom: {{- include "urumi.secretRef" (dict "ctx" .ctx "key" "mariadb-password") | nindent 4 }}
{{- end }}

{{/* Non-secret store-provisioning config, shared by the gateway (limits/naming) and the provisioner (everything). */}}
{{- define "urumi.storesConfig" -}}
{{- $s := .Values.stores -}}
{{- toJson (dict
  "enabled" $s.enabled "maxPerUser" $s.maxPerUser "provisionTimeoutSeconds" $s.provisionTimeoutSeconds
  "namespacePrefix" $s.namespacePrefix "domain" (default .Values.domain $s.domain) "scheme" .Values.scheme
  "gatewayNamespace" .Release.Namespace "filesDir" "/etc/urumi/store-files"
  "reservedSlugs" (list "app" "store" "www" "gateway" "woocommerce" "notes" "utils")
  "images" (dict "wordpress" .Values.images.wordpress "wpcli" .Values.images.wpcli "mariadb" .Values.images.mariadb
                 "wooMcp" .Values.images.wooMcp "pullPolicy" .Values.images.pullPolicy)
  "store" (dict "currency" .Values.store.currency "country" .Values.store.country "timezone" .Values.store.timezone
                "woocommerceVersion" .Values.store.woocommerceVersion "adminUser" .Values.store.adminUser)
  "ingress" (dict "className" .Values.ingress.className "controllerNamespace" .Values.networkPolicy.ingressControllerNamespace
                  "tls" (dict "enabled" .Values.ingress.tls.enabled "clusterIssuer" .Values.ingress.tls.clusterIssuer))
  "storage" (dict "className" .Values.storage.className "mariadb" $s.storage.mariadb "wordpress" $s.storage.wordpress)
  "quota" $s.quota "resources" $s.resources) -}}
{{- end -}}
