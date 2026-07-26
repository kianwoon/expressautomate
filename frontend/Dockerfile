# syntax=docker/dockerfile:1

FROM node:22-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

# The landing page is a static export, so it needs no Node process at runtime.
# nginx idles in a few MB, which is what makes eco-nano (256 MB) comfortable.
FROM nginx:1.27-alpine
COPY --from=build /app/out /usr/share/nginx/html
COPY nginx.conf /etc/nginx/templates/default.conf.template
ENV PORT=8000
EXPOSE 8000
