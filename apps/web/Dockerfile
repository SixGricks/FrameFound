# FrameFound web frontend (Next.js standalone output).
FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:22-alpine
# The runtime runs `node server.js` and nothing else, so the bundled package
# manager is pure attack surface — and the tar it vendors is where the image's
# only CRITICAL finding came from (CVE-2026-59873). Removing it fixes that at
# the source instead of waiting for a base image with a patched npm.
RUN rm -rf /usr/local/lib/node_modules/npm /usr/local/bin/npm /usr/local/bin/npx
RUN addgroup -S framefound && adduser -S framefound -G framefound
WORKDIR /app
COPY --from=build --chown=framefound:framefound /app/.next/standalone ./
COPY --from=build --chown=framefound:framefound /app/.next/static ./.next/static
USER framefound
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0
CMD ["node", "server.js"]
