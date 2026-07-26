--
-- PostgreSQL database dump
--

\restrict QuM3jQlcapGHxL5tMTMwuguofGt6Ce6uwDSrIP07HbMH9fdueaOiOtt7nUFFDrp

-- Dumped from database version 16.14
-- Dumped by pg_dump version 16.14

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: aidataops
--

-- *not* creating schema, since initdb creates it


ALTER SCHEMA public OWNER TO aidataops;

--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: aidataops
--

COMMENT ON SCHEMA public IS '';


--
-- Name: source_type_enum; Type: TYPE; Schema: public; Owner: aidataops
--

CREATE TYPE public.source_type_enum AS ENUM (
    'postgres',
    'mysql',
    'rest_api',
    'csv_upload',
    's3',
    'other'
);


ALTER TYPE public.source_type_enum OWNER TO aidataops;

--
-- Name: task_run_status_enum; Type: TYPE; Schema: public; Owner: aidataops
--

CREATE TYPE public.task_run_status_enum AS ENUM (
    'pending',
    'running',
    'success',
    'failed'
);


ALTER TYPE public.task_run_status_enum OWNER TO aidataops;

--
-- Name: task_type_enum; Type: TYPE; Schema: public; Owner: aidataops
--

CREATE TYPE public.task_type_enum AS ENUM (
    'sync',
    'transform',
    'export',
    'other'
);


ALTER TYPE public.task_type_enum OWNER TO aidataops;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: aidataops
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


ALTER TABLE public.alembic_version OWNER TO aidataops;

--
-- Data for Name: alembic_version; Type: TABLE DATA; Schema: public; Owner: aidataops
--

COPY public.alembic_version (version_num) FROM stdin;
c3d4e5f6a7b8
\.


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: aidataops
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: aidataops
--

REVOKE USAGE ON SCHEMA public FROM PUBLIC;


--
-- PostgreSQL database dump complete
--

\unrestrict QuM3jQlcapGHxL5tMTMwuguofGt6Ce6uwDSrIP07HbMH9fdueaOiOtt7nUFFDrp

