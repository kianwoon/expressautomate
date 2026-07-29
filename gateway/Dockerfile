# syntax=docker/dockerfile:1
#
# The WA gateway gets its OWN image — it must never run on the shared backend
# image (plan §12.3). Build context is `gateway/`, not the repo root, so a
# backend or frontend edit cannot invalidate a single layer here.

FROM node:22-alpine AS deps
WORKDIR /gateway
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-alpine AS build
WORKDIR /gateway
COPY --from=deps /gateway/node_modules ./node_modules
COPY package.json package-lock.json tsconfig.json tsconfig.build.json ./
COPY src ./src
# tsconfig.build.json excludes *.test.ts, so tests never ship.
RUN npm run build

FROM node:22-alpine AS runtime
WORKDIR /gateway
ENV NODE_ENV=production
# Production dependency tree only: tsx and typescript stay out of the image.
COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force
COPY --from=build /gateway/dist ./dist

# Not root. Baileys auth state (P2) is decrypted in this process.
USER node

# Documentation only — Koyeb routes to the port set in the service definition,
# and WA_GATEWAY_PORT must agree with it.
EXPOSE 7300

# Koyeb health check: GET /health on this port. Set by hand on the service;
# a 404 there leaves the deploy PENDING until CI times out (CLAUDE.md).
CMD ["node", "dist/index.js"]
