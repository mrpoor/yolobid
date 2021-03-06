from operator import itemgetter
from decimal import Decimal
from django.forms import model_to_dict
import numpy
from dashboard.service.mixin_classes import ConvertMixin
numpy.set_printoptions(threshold=numpy.inf)
import pandas
# from entities.league_of_legends_entities import Game, Player
from statsmodels.discrete.discrete_model import Poisson
from dashboard.models import Game, Player, ProcessedTeamStatsDf
import pickle

__author__ = 'Greg'


class PredictPlayerStats(ConvertMixin):

    def __init__(self, engine, player_name, stat_to_predict, opposing_team_name,
                 predictor_stats=('csum_min_kills', 'csum_min_minions_killed'),
                 defense_predictor_stats=('csum_prev_min_allowed_kills', 'csum_prev_min_allowed_assists'),
                 game_range=None):
        self.engine = engine
        self.player_name = player_name
        self.stat_to_predict = stat_to_predict
        if predictor_stats:
            self.predictor_stats = ('csum_prev_min_kills', 'csum_prev_min_minions_killed')
        else:
            self.predictor_stats = ('csum_prev_min_kills', 'csum_prev_min_minions_killed')
        role_stats = ('Jungler', 'Mid', 'Coach', 'Support', 'AD', 'Sub', 'Top')
        self.predictor_stats = self.predictor_stats + defense_predictor_stats + role_stats
        self.opposing_team_name = opposing_team_name
        self.player_stats_table_name = 'player_stats_df'
        self.processed_player_stars_table_name = 'processed_player_stats_df'
        self.key_stats = ('kills', 'deaths', 'assists', 'minions_killed', 'gold',
                          'k_a', 'a_over_k')
        self.game_range = game_range
        self._process_player_stats_and_train()

    def _process_player_stats_and_train(self):
        processed_player_stats_df = self._get_processed_player_stats_in_df()
        self.latest_predictor_numpy_array = self._get_latest_player_stats_numpy_array(processed_player_stats_df)
        print('latest predictors numpy array {}'.format(self.latest_predictor_numpy_array))
        predictors, y_array = self._get_predictors_in_numpy_arrays(processed_player_stats_df)
        self._train_model(predictors, y_array)

    def _get_latest_player_stats_numpy_array(self, processed_player_stats_df):
        player_id = self._get_player_id_by_player_name(self.player_name)
        player_stats_df = processed_player_stats_df[processed_player_stats_df['player_id'] == player_id]
        latest_player_stats_df = player_stats_df.sort(['game_id'], ascending=False).head(1)
        dict_player = latest_player_stats_df.to_dict('records')[0]
        player_predictor_stats = []
        for predictor_stat in self.predictor_stats:
            # print('processing predictor stat {}'.format(predictor_stat))
            player_predictor_stats.append(dict_player[predictor_stat])
        latest_predictor_numpy_array = numpy.array([player_predictor_stats])
        return latest_predictor_numpy_array

    def _get_predictors_in_numpy_arrays(self, processed_player_stats_df):
        player_game_records = self._get_predictors(processed_player_stats_df)
        game_list = []
        y_array_list = []
        for player_game_record in player_game_records:
            game_predictor_stats = []
            if not (numpy.isnan(player_game_record['csum_prev_min_kills'])
                    or numpy.isnan(player_game_record['csum_prev_min_allowed_kills'])):
                if player_game_record['csum_prev_min_assists'] != 0:
                    prev_predictor_stats = self._convert_predictors_to_prev_csum(self.predictor_stats)
                    for prev_predictor_stat in prev_predictor_stats:
                        game_predictor_stats.append(player_game_record[prev_predictor_stat])
                    game_list.append(game_predictor_stats)
                    y_array_list.append(player_game_record['y_element'])
        predictors = numpy.array(game_list)
        y_array = numpy.array([y_array_list])
        return predictors, y_array

    def _get_predictors(self, processed_player_stats_df):
        player_game_records = processed_player_stats_df.to_dict('records')
        player_game_records.sort(key=itemgetter('game_id'))
        for player_game_record in player_game_records:
            player_game_record['y_element'] = player_game_record[self.stat_to_predict]
        return player_game_records

    def _train_model(self, predictors, y_array):
        y_1darray = numpy.squeeze(y_array)
        self.poisson = Poisson(y_1darray, predictors)
        self.pos_result = self.poisson.fit(method='bfgs')

    def _get_game_ids_from_database(self):
        game_ids_row = Game.objects.values_list('id', flat=True)
        game_ids = [game for game in game_ids_row]
        return game_ids

    def _get_lastest_processed_team_stats_by_name(self):
        return ProcessedTeamStatsDf.objects.filter(name=self.opposing_team_name).order_by('-id').first()

    def _get_game_by_ids(self, game_ids):
        return Game.objects.filter(id__in=game_ids)

    def _get_player_id_by_player_name(self, player_name):
        player = Player.objects.filter(name=player_name)
        return player[0].id

    def _get_processed_player_stats_in_df(self):
        game_ids = self._get_game_ids_from_database()
        last_game_number = game_ids[-1]
        has_processed_team_stats_table = self.engine.has_table(self.processed_player_stars_table_name)
        if has_processed_team_stats_table:
            df_game_stats = pandas.read_sql(self.player_stats_table_name, self.engine)
            df_game_stats_all = df_game_stats[df_game_stats.game_id.isin(game_ids)]
            # Using game_numbers here since we need the last few games to check.
            max_game_id_cached = df_game_stats_all['game_id'].max()
            max_index_cached = df_game_stats_all['index'].max()
            if pandas.isnull(max_game_id_cached):
                max_game_id_cached = game_ids[0]
            # Check if all the game numbers have been cached,
            # if not return what game to start form and what game to end from.
            if max_game_id_cached != last_game_number:
                # Get the index of the max_game_id
                max_game_id_index = game_ids.index(max_game_id_cached)
                # Trim down the list to only the games that need to be retrieved,
                # start from the max_id + 1 because we don't
                # want to count max_id we already have it
                game_ids_to_find = game_ids[max_game_id_index:]
                games = self._get_game_by_ids(game_ids_to_find)
                player_stats_df = self._get_player_stats_in_df(games, max_index_cached)
                self._insert_into_player_stats_df_tables(player_stats_df)
            else:
                # If everything was cached return cached as true and just return the last numbers
                # I could do this part better.
                print("everything cached no need to retrieve from api")
        else:
            _get_player_stats_in_df = 0
            # Table did not exist, have to get all
            games = self._get_game_by_ids(game_ids)
            player_stats_df = self._get_player_stats_in_df(games, _get_player_stats_in_df)
            print('table does not exist inserting full table')
            self._insert_into_player_stats_df_tables(player_stats_df)
            print('table inserted')
        if self.game_range == '5':
            processed_player_stats_df = pandas.read_sql('select * from processed_player_stats_df_limit_5',
                                                              con=self.engine)
        elif self.game_range == '10':
            processed_player_stats_df = pandas.read_sql('select * from processed_player_stats_df_limit_10',
                                                              con=self.engine)
        else:
            processed_player_stats_df = pandas.read_sql_table(self.processed_player_stars_table_name, self.engine)
        return processed_player_stats_df

    def _process_player_stats_df(self, player_stats_df):
        player_stats_df = player_stats_df.sort(['game_id', 'player_id'])
        key_stats = ['game_length_minutes'] + (list(self.key_stats))
        player_stats_df['clean_kills'] = player_stats_df['kills']
        player_stats_df.ix[player_stats_df.clean_kills == 0, 'clean_kills'] = 1
        player_stats_df['k_a'] = \
            player_stats_df['kills'] + player_stats_df['assists']
        player_stats_df['a_over_k'] = \
            player_stats_df['assists'] / player_stats_df['clean_kills']
        player_stats_for_pivot = player_stats_df[['player_name', 'role']]
        player_stats_for_pivot['value'] = 1
        player_pivot_df = player_stats_for_pivot.pivot_table(index='player_name', columns='role', values='value')
        player_pivot_df.fillna(0, inplace=True)
        player_pivot_df.reset_index(inplace=True)
        player_stats_df = pandas.merge(player_stats_df, player_pivot_df, on='player_name')
        for key_stat in key_stats:
            print('doing key stats {}'.format(key_stat))
            player_stats_df['csum_{}'.format(key_stat)] = player_stats_df.groupby(by='player_id')[key_stat].cumsum()
            player_stats_df['csum_prev_{}'.format(key_stat)] = \
                player_stats_df['csum_{}'.format(key_stat)] - player_stats_df[key_stat]
            # player_stats_df['csum_prev_avg_{}'.format(key_stat)] = \
            #     player_stats_df['csum_prev_{}'.format(key_stat)] / player_stats_df['csum_prev_game_number']
            player_stats_df['per_min_{}'.format(key_stat)] = player_stats_df[key_stat] / \
                                                             player_stats_df['game_length_minutes']
            if key_stat not in ['game_number', 'game_length_minutes']:
                print('doing stats not game_number {}'.format(key_stat))
                player_stats_df['csum_min_{}'.format(key_stat)] = \
                    player_stats_df['csum_{}'.format(key_stat)] / player_stats_df['csum_game_length_minutes']
                player_stats_df['csum_prev_min_{}'.format(key_stat)] = \
                    player_stats_df['csum_prev_{}'.format(key_stat)] / player_stats_df['csum_prev_game_length_minutes']
                player_stats_df['csum_prev_min_{}'.format(key_stat)].fillna(0, inplace=True)
            player_stats_df = player_stats_df.sort(['game_id'])
        return player_stats_df

    def _get_player_stats_in_df(self, games, max_index_cached):
        player_stats_df = None
        for game in games:
            players_stats = self._convert_game_to_player_stats_df(game)
            if player_stats_df is None:
                player_stats_df = pandas.DataFrame(players_stats, index=list(range(max_index_cached, (max_index_cached + 10))))
            else:
                single_game_player_stats_df = pandas.DataFrame(players_stats, index=list(range(max_index_cached, (max_index_cached + 10))))
                player_stats_df = player_stats_df.append(single_game_player_stats_df)
            max_index_cached += 10
        return player_stats_df

    def _convert_game_to_player_stats_df(self, game):
        players_stats = game.playerstats_set.all()
        players_stats_dict = game.playerstats_set.all().values()
        player_stats_list = []
        for player_stats, player_stats_dict in zip(players_stats, players_stats_dict):
            player_stats_dict['game_length_minutes'] = float(game.game_length_minutes)
            player_stats_dict['gold'] = float(player_stats_dict['gold'])
            player_stats_dict['player_name'] = player_stats.player.name
            self._populate_player_stats_with_defense_stats(player_stats_dict, player_stats, game)
            player_stats_list.append(player_stats_dict)
        return player_stats_list

    def _populate_player_stats_with_defense_stats(self, player_stats_dict, player_stats, game):
        current_team = player_stats.team
        processed_team_stats_dict = game.processedteamstatsdf_set.exclude(team_name=current_team).values()[0]
        for key_stat in self.key_stats:
            player_stats_dict['csum_prev_min_allowed_{}'.format(key_stat)] = \
                processed_team_stats_dict['csum_prev_min_allowed_{}'.format(key_stat)]
            player_stats_dict['csum_min_allowed_{}'.format(key_stat)] = \
                processed_team_stats_dict['csum_min_allowed_{}'.format(key_stat)]

    def _insert_into_player_stats_df_tables(self, player_stats_df):
        player_stats_df.to_sql(self.player_stats_table_name, self.engine, if_exists='append')
        # Could be optimized kinda a hack
        player_stats_df = pandas.read_sql("select ps.*, p.role, p.image from player_stats_df ps, player p "
                                          "where ps.player_id = p.id", self.engine)
        processed_team_stats_df = self._process_player_stats_df(player_stats_df)
        processed_team_stats_df.to_sql(self.processed_player_stars_table_name, self.engine, if_exists='append')

    def predict_player_stat(self):
        #reshaped_numpy_array = numpy.reshape(self.latest_predictor_numpy_array, 3,1)
        probability_in_numpy_array = self.poisson.predict(self.pos_result.params, self.latest_predictor_numpy_array)
        return {self.player_name: probability_in_numpy_array}
