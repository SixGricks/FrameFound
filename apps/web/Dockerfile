# MediaHub web frontend (Next.js standalone output).
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci || npm install   # TODO(m1): commit package-lock.json, then `npm ci` only
COPY . .
RUN npm run build

FROM node:20-alpine
RUN addgroup -S mediahub && adduser -S mediahub -G mediahub
WORKDIR /app
COPY --from=build --chown=mediahub:mediahub /app/.next/standalone ./
COPY --from=build --chown=mediahub:mediahub /app/.next/static ./.next/static
USER mediahub
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0
CMD ["node", "server.js"]
