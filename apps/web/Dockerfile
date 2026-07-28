# FrameFound web frontend (Next.js standalone output).
FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:20-alpine
RUN addgroup -S framefound && adduser -S framefound -G framefound
WORKDIR /app
COPY --from=build --chown=framefound:framefound /app/.next/standalone ./
COPY --from=build --chown=framefound:framefound /app/.next/static ./.next/static
USER framefound
EXPOSE 3000
ENV PORT=3000 HOSTNAME=0.0.0.0
CMD ["node", "server.js"]
