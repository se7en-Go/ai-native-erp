import tseslint from "typescript-eslint";

const FORBIDDEN_DB_MODULES = [
  "pg",
  "pg-pool",
  "postgres",
  "asyncpg",
  "drizzle-orm",
  "prisma",
  "@prisma/client",
  "kysely",
  "better-sqlite3",
  "sqlite3",
  "mysql2",
  "typeorm",
  "sequelize",
  "knex",
  "mongoose",
  "ioredis",
  "redis",
  "sqlalchemy",
];

export default tseslint.config(
  {
    ignores: [".next/**", "node_modules/**", "src/lib/api-client/**"],
  },
  ...tseslint.configs.recommended,
  {
    files: ["src/**/*.{ts,tsx}"],
    rules: {
      "@typescript-eslint/no-unused-vars": ["error", { argsIgnorePattern: "^_" }],
      "@typescript-eslint/no-explicit-any": "warn",
      "no-restricted-imports": [
        "error",
        {
          paths: FORBIDDEN_DB_MODULES.map((name) => ({
            name,
            message:
              "Constraint #3 violation: apps/web must remain a zero-SQL thin UI/BFF shell. Call FastAPI via /api/bff/v1/* instead.",
          })),
          patterns: [
            {
              group: ["**/erp_core/**", "drizzle-orm/*", "@prisma/*"],
              message: "Constraint #3 violation: direct backend/ORM imports are forbidden in apps/web.",
            },
          ],
        },
      ],
    },
  }
);
