from datetime import datetime

from app import (
    collect_ob_tenant_detail,
    get_pool,
    get_tenant_with_cluster,
    local_now,
    one,
    record_collection_job,
    store_tenant_detail,
    all_rows,
    build_tenant_login_user,
    execute,
)


def schedule_due(schedule, now):
    run_time = (schedule.get("run_time") or "07:00")[:5]
    current_time = now.strftime("%H:%M")
    if current_time != run_time:
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


def collect_one(db, schedule):
    tenant_id = schedule["tenant_id"]
    tenant = get_tenant_with_cluster(db, tenant_id)
    connection = one(db, "select * from tenant_connections where tenant_id = :tenant_id", {"tenant_id": tenant_id})
    if not tenant or not connection or not connection.get("tenant_password"):
        return
    started = local_now()
    collected = collect_ob_tenant_detail(
        {
            "endpoint": tenant["endpoint"],
            "port": tenant["port"],
            "tenant_name": tenant["name"],
            "tenant_user": build_tenant_login_user(connection["tenant_user"], tenant),
            "tenant_password": connection["tenant_password"],
            "database": connection.get("database_name") or "",
        }
    )
    stats = store_tenant_detail(db, tenant_id, collected)
    record_collection_job(db, tenant["cluster_id"], "tenant_detail_schedule", "success", str(stats), started)
    execute(
        db,
        "update tenant_collect_schedules set last_run_at = systimestamp, updated_at = systimestamp where id = :id",
        {"id": schedule["id"]},
    )


def main():
    db = get_pool().acquire()
    try:
        now = datetime.now()
        schedules = all_rows(db, "select * from tenant_collect_schedules where enabled = 1")
        for schedule in schedules:
            if schedule_due(schedule, now):
                collect_one(db, schedule)
        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    main()
