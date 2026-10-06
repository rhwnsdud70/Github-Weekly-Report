def transmission_logger(database, run_id):
    def log(message, attempt, status, error):
        database.log_transmission(run_id,message.kind,message.rank,message.part,attempt,status,error)
    return log


def transmission_status(results):
    successes = sum(result.status in {'SUCCESS','RETRY_SUCCESS'} for result in results)
    if successes == len(results):
        return 'SUCCESS'
    return 'PARTIAL_SUCCESS' if successes else 'FAILED'
