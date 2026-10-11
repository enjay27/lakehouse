# The Polaris console's image (ADR-028), built on the gate by `gates/gate.py console-image`:
# apache/polaris-tools at the commit in source.json, its console/ as the build context, with
# this directory's package-lock.json copied in. Upstream's console/docker/Dockerfile, except:
# - `npm ci` from our lockfile, where upstream runs `npm install` without one: every package
#   was released at least two weeks before the lockfile was made, and npm checks each against
#   its sha512. No install scripts run: the only ones are esbuild's, which finds its binary
#   through @esbuild/linux-x64 without it, and fsevents', which is macOS only.
#   --legacy-peer-deps: npm 10.9 writes a lockfile its own `npm ci` refuses, because
#   @hookform/resolvers' optional peer ajv ^8 meets eslint's hoisted ajv 6; without the flag
#   `npm ci` re-resolves that peer instead of installing the lockfile. With it, `npm ci`
#   installs exactly the lockfile's packages. The console uses only the zod resolver.
# - the same base images, pinned by digest (registry.access.redhat.com manifest-list digest,
#   recomputed, read 2026-10-11).

# Build stage
FROM registry.access.redhat.com/ubi9/nodejs-22-minimal:1-1779828907@sha256:75a2c4753c2475d715e31304ec1effef61770713e6e9fdafdcb80351dbdf3ba5 AS builder

# Default environment variables (can be overridden at runtime)
ENV VITE_POLARIS_API_URL=http://polaris:8181
ENV VITE_POLARIS_REALM=POLARIS
ENV VITE_POLARIS_PRINCIPAL_SCOPE=PRINCIPAL_ROLE:ALL
ENV VITE_OAUTH_TOKEN_URL=http://polaris:8181/api/catalog/v1/oauth/tokens
ENV VITE_POLARIS_REALM_HEADER_NAME=Polaris-Realm

USER root

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --ignore-scripts --legacy-peer-deps --no-audit --no-fund

COPY . .

RUN npm run build

# Production stage
FROM registry.access.redhat.com/ubi9/nginx-126:1-1779858291@sha256:7577c008ddb13b5c79a747b356cdf798ee6ab18b6db5234716e6cac2b9b652bb
USER root
RUN groupadd --gid 10001 polaris && \
    useradd --uid 10000 --gid polaris polaris
COPY LICENSE-BUNDLE /LICENSE
COPY NOTICE /NOTICE

COPY docker/nginx.conf /opt/app-root/etc/nginx.default.d/default.conf
COPY --chown=10000:10001 --chmod=744 docker/generate-config.sh /generate-config.sh
COPY --from=builder /app/dist /opt/app-root/src

RUN chown -R polaris:polaris /opt/app-root
EXPOSE 8080
RUN chown -R polaris:polaris /var/log/nginx \
    && chown -R polaris:polaris /etc/nginx/conf.d \
    && chown -R polaris:polaris /var/lib/nginx \
    && touch /var/run/nginx.pid \
    && chown -R polaris:polaris /var/run/nginx.pid
USER polaris
ENTRYPOINT ["/generate-config.sh"]
