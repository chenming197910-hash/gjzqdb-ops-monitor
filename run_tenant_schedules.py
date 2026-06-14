import logging
from datetime import datetime

from app import (
    collect_cluster_assets,
    collect_ob_cluster,
    collect_ob_tenant_detail,
    get_pool,
    get_tenant_with_cluster,
    local_now,
    one,
    record_collection_job,
    run_sys_tenant_checks,
    store_tenant_detail,
    all_rows,
    build_tenant_collect_user,
    build_tenant_service_name,
    execute,
    log_event,
)


def schedule_due(schedule, now):
    run_time = (schedule.get("run_time") or "07:00")[:5]
    current_time = now.strftime("%H:%M")
    if current_time < run_time:
        return False
    last_run = schedule.get("last_run_at")
    if last_run and str(last_run)[:10] == now.strftime("%Y-%m-%d"):
        return False
    frequency = schedule.get("frequency") or "daily"
    if frequency == "daily":
        return True
    if frequency == "workday":
        return now.isoweekday() <= 5
    if frequency == "weekly":
        return now.isoweekday() == int(schedule.get("day_of_week") or 1)
    if frequency == "monthly":
        return now.day == int(schedule.get("day_of_month") or 1)
    return False


def collect_one_cluster(db, schedule):
    cluster_id = schedule["cluster_id"]
    cluster = one(
        db,
        "select id, name, endpoint, port, sys_user, sys_password from clusters where id = :id",
        {"id": cluster_id},
    )
    if not cluster or not cluster.get("sys_password"):
        return
    started = local_now()
    try:
        collected = collect_ob_cluster(cluster)
        stats = collect_cluster_assets(db, cluster_id, collected)
        status = "warning" if stats.get("warnings") else "success"
        message = str(stats)
    except Exception as exc:
        status = "failed"
        message = str(exc)[:1000]
        log_event(
            logging.ERROR,
            "scheduled_ob_cluster_collect_failed",
            cluster_id=cluster_id,
            cluster_name=cluster.get("name"),
            target=f"{cluster.get('endpoint')}:{cluster.get('port')}",
            user=cluster.get("sys_user"),
            error=message,
            exc_info=True,
        )
    record_collection_job(db, cluster_id, "ob_cluster_schedule", status, message, started)
    execute(
        db,
        "update cluster_collect_schedules set last_run_at = systimestamp, updated_at = systimestamp where id = :id",
        {"id": schedule["id"]},
    )


def collect_one(db, schedule):
    tenant_id = schedule["tenant_id"]
    tenant = get_tenant_with_cluster(db, tenant_id)
    connection = one(db, "select * from tenant_connections where tenant_id = :tenant_id", {"tenant_id": tenant_id})
    if not tenant or not connection or not connection.get("tenant_password"):
        return
    started = local_now()
    try:
        collected = collect_ob_tenant_detail(
            {
                "endpoint": tenant["endpoint"],
                "port": tenant["port"],
                "tenant_name": tenant["name"],
                "tenant_mode": tenant["tenant_mode"],
                "tenant_user": build_tenant_collect_user(connection["tenant_user"], tenant),
                "tenant_password": connection["tenant_password"],
                "database": build_tenant_service_name(connection.get("database_name"), tenant),
            }
        )
        stats = store_tenant_detail(db, tenant_id, collected)
        status = "warning" if stats.get("warnings") else "success"
        message = str(stats)
    except Exception as exc:
        status = "failed"
        message = str(exc)[:1000]
        log_event(
            logging.ERROR,
            "scheduled_tenant_detail_collect_failed",
            tenant_id=tenant_id,
            tenant_name=tenant["name"],
            tenant_mode=tenant["tenant_mode"],
            target=f"{tenant['endpoint']}:{tenant['port']}",
            user=build_tenant_collect_user(connection["tenant_user"], tenant),
            error=message,
            exc_info=True,
        )
    record_collection_job(db, tenant["cluster_id"], "tenant_detail_schedule", status, message, started)
    execute(
        db,
        "update tenant_collect_schedules set last_run_at = systimestamp, updated_at = systimestamp where id = :id",
        {"id": schedule["id"]},
    )


def main():
    db = get_pool().acquire()
    try:
        now = datetime.now()
        cluster_schedules = all_rows(db, "select * from cluster_collect_schedules where enabled = 1")
        for schedule in cluster_schedules:
            if schedule_due({**schedule, "frequency": "daily"}, now):
                collect_one_cluster(db, schedule)
        schedules = all_rows(db, "select * from tenant_collect_schedules where enabled = 1")
        for schedule in schedules:
            if schedule_due(schedule, now):
                collect_one(db, schedule)
        run_sys_tenant_checks(db, force=False)
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
