"""Fast exact objective deltas for a fixed common rendezvous order."""
import numpy as np


class RouteScores:
    def __init__(self, matrix, sequences, teams, service, objective, mandatory_nodes=()):
        self.matrix, self.teams, self.service, self.objective = matrix, teams, service, objective
        self.mandatory = set(mandatory_nodes)
        self.order = tuple(n for n in sequences[0] if n in self.mandatory)
        if any(tuple(n for n in seq if n in self.mandatory) != self.order for seq in sequences):
            raise ValueError('各队集合顺序必须相同')
        self.count, self.last = len(teams), len(self.order)
        profiles = [self.profile(i, seq) for i, seq in enumerate(sequences)]
        self.costs = np.array([p[0] for p in profiles])
        self.work = np.array([p[1] for p in profiles])
        self.stages = [p[2] for p in profiles]
        self.maxima = self.work.max(axis=0)
        self.release = self.maxima[:-1].sum()
        self.finishes = self.release+self.work[:, -1]
        self.total_cost = float(self.costs.sum())
        self.total_finish = float(self.finishes.sum())
        self.max_finish = float(self.finishes.max())
        second = np.sort(self.work, axis=0)[-2] if self.count > 1 else np.full(self.last+1, -np.inf)
        self.max_except = np.where(self.work == self.maxima, second, self.maxima)
        last_second = np.sort(self.finishes)[-2] if self.count > 1 else -np.inf
        self.finish_except = np.where(self.finishes == self.max_finish, last_second, self.max_finish)

    def profile(self, team, sequence):
        work = np.zeros(self.last+1)
        work[0] = self.teams[team].get('departureSec', 0)
        stage, cost, stages = 0, 0., []
        for a, b in zip(sequence, sequence[1:]):
            stages.append(stage)
            weight = self.matrix[a, b]
            cost += weight
            work[stage] += weight/self.teams[team]['speedMps']
            if b != len(self.matrix)-1:
                work[stage] += self.service[team][b-1]
            if b in self.mandatory:
                stage += 1
        return float(cost), work, stages

    def score(self):
        if self.objective == 'min_distance':
            return (self.total_cost,)
        return (self.max_finish, self.total_finish) if self.objective == 'min_max_finish_then_sum' else (self.total_finish,)

    def insertion(self, team, stage, distance_delta, service_delta):
        if self.objective == 'min_distance':
            return (self.total_cost+distance_delta,)
        change = distance_delta/self.teams[team]['speedMps']+service_delta
        if stage < self.last:
            release_delta = max(self.max_except[team, stage], self.work[team, stage]+change)-self.maxima[stage]
            maximum, total = self.max_finish+release_delta, self.total_finish+self.count*release_delta
        else:
            maximum = max(self.finish_except[team], self.finishes[team]+change)
            total = self.total_finish+change
        return (float(maximum), float(total)) if self.objective == 'min_max_finish_then_sum' else (float(total),)

    def replacements(self, replacements):
        cost = self.total_cost
        work = self.work.copy()
        for team, sequence in replacements.items():
            route_cost, route_work, _ = self.profile(team, sequence)
            cost += route_cost-self.costs[team]
            work[team] = route_work
        if self.objective == 'min_distance':
            return (float(cost),)
        finish = work[:, :-1].max(axis=0).sum()+work[:, -1]
        return (float(finish.max()), float(finish.sum())) if self.objective == 'min_max_finish_then_sum' else (float(finish.sum()),)


def improves(candidate, current, tolerance=1e-8):
    for a, b in zip(candidate, current):
        if a < b-tolerance:
            return True
        if a > b+tolerance:
            return False
    return False
