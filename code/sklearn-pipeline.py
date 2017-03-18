import os
import sys
import operator
import math
import pickle
import numpy as np
import pandas as pd
import xgboost as xgb
import datetime
from scipy import sparse
from sklearn.metrics import log_loss
from sklearn.feature_extraction.text import TfidfVectorizer, CountVectorizer
from sklearn.pipeline import Pipeline,FeatureUnion
from sklearn.base import BaseEstimator
from sklearn.base import TransformerMixin
from sklearn.preprocessing import LabelEncoder, OneHotEncoder, MinMaxScaler
from sklearn.model_selection import train_test_split

np.random.seed(42)

class ManagerSkill(BaseEstimator, TransformerMixin):
    def __init__(self, threshold = 5):
        self.threshold = threshold
        
    def _reset(self):
        if hasattr(self, 'mapping_'):
            self.mapping_ = {}
            self.mean_skill_ = 0.0
        
    def fit(self,X,y):
        self._reset()
        temp = pd.concat([X.manager_id,pd.get_dummies(y)], axis = 1).\
            groupby('manager_id').mean()
        temp.rename(columns=\
            {0: 'high_frac', 1: 'medium_frac', 2: 'low_frac'},\
            inplace=True)

        temp['count'] = X.groupby('manager_id').count().iloc[:,0]
        temp['manager_skill'] = temp['high_frac']*2 + temp['medium_frac']
        mean = temp.loc[temp['count'] >= self.threshold, 'manager_skill'].mean()
        temp.loc[temp['count'] < self.threshold, 'manager_skill'] = mean
        
        self.mapping_ = temp[['manager_skill']]
        self.mean_skill_ = mean
        return self
        
    def transform(self, X):
        X = pd.merge(left = X,right = self.mapping_,how = 'left',\
            left_on = 'manager_id',right_index = True)
        X['manager_skill'].fillna(self.mean_skill_, inplace = True)
        return X[['manager_skill']]

class CategoricalTransformer(BaseEstimator, TransformerMixin):

    def __init__(self, column_name, k):
        self.threshold = k
        self.column_name = column_name

    def _reset(self):
        if hasattr(self, 'mapping_'):
            self.mapping_ = {}
            self.glob_med = 0
            self.glob_high = 0
            self.glob_low = 0

    def fit(self, X):
        self._reset()

        tmp = X.groupby([self.column_name, 'interest_level']).size().\
            unstack().reset_index()
        tmp = tmp.fillna(0)

        tmp['record_count'] = tmp['high'] + tmp['medium'] + tmp['low']
        tmp['high_share'] = tmp['high']/tmp['record_count']
        tmp['med_share'] = tmp['medium']/tmp['record_count']

        self.glob_high = tmp['high'].sum()/tmp['record_count'].sum()
        self.glob_med = tmp['medium'].sum()/tmp['record_count'].sum()

        tmp['lambda'] = tmp['record_count'].\
            apply(lambda x: 1.0 / (1.0 + math.exp(self.threshold-x)))

        tmp['w_high_'+self.column_name] = tmp[['high_share','lambda']].\
            apply(lambda row:\
            row['lambda']*row['high_share']+(1.0-row['lambda'])*self.glob_high,\
            axis=1)
        
        tmp['w_med_'+self.column_name] = tmp[['med_share','lambda']].\
            apply(lambda row:\
            row['lambda']*row['med_share']+(1.0-row['lambda'])*self.glob_med,\
            axis=1)

        tmp['w_high_'+self.column_name] = tmp['w_high_'+self.column_name].\
            apply(lambda x : x*(1.0 + 0.01*(np.random.uniform()-0.5)))
        tmp['w_med_' + self.column_name] = tmp['w_med_' + self.column_name].\
            apply(lambda x : x*(1.0 + 0.01*(np.random.uniform()-0.5)))

        self.mapping_ = tmp[['w_high_' + self.column_name,\
            'w_med_' + self.column_name,  self.column_name]]

        return self

    def transform(self, X):
        X = X.merge(self.mapping_.reset_index(),\
            how = 'left',\
            left_on = self.column_name,\
            right_on = self.column_name)
        del X['index'] #remove the side-effect-of-merge column
        X['w_high_' + self.column_name] = X['w_high_' + self.column_name].\
        apply(lambda x: x if not np.isnan(x) else self.glob_high)
        X['w_med_' + self.column_name] = X['w_med_' + self.column_name].\
        apply(lambda x: x if not np.isnan(x) else self.glob_med)
        return X

class Debugger(BaseEstimator, TransformerMixin):
    def fit(self, x, y=None):
        return self

    def transform(self, X):
        print X.shape
        return X

class ColumnExtractor(BaseEstimator, TransformerMixin):
    def __init__(self, fields):
        self.fields = fields
        
    def fit(self, X,y):
        return self
        
    def transform(self, X):
        return X[self.fields]

class ApartmentFeaturesVectorizer(BaseEstimator, TransformerMixin):
    def __init__(self, num_features = 400):
        self.num_features = num_features
        
    def fit(self, X,y):
        self.tfidf = CountVectorizer(stop_words='english',\
            max_features=self.num_features)
        
        self.tfidf.fit(X['features'])
        return self
        
    def transform(self, X):
        X_sparse = self.tfidf.transform(X['features'])
        return X_sparse

def get_importance(gbm):
    """
    Getting relative feature importance
    """
    importance = pd.Series(gbm.get_fscore()).sort_values(ascending=False)
    importance = importance/1.e-2/importance.values.sum()
    return importance

def create_submission(score, pred, model, importance):
    """
    Saving model, features and submission
    """
    ouDir = '../output/'
    
    now = datetime.datetime.now()
    scrstr = "{:0.4f}_{}".format(score,now.strftime("%Y-%m-%d-%H%M"))
    
    mod_file = ouDir + '.model_' + scrstr + '.model'
    print('Writing model: ', mod_file)
    model.save_model(mod_file)
    
    sub_file = ouDir + 'submit_' + scrstr + '.csv'
    print('Writing submission: ', sub_file)
    pred.to_csv(sub_file, index=False)

def runXGB(train_X, train_y, test_X, test_y=None, feature_names=None,\
    seed_val=0,
    num_rounds=2000):
    param = {}
    param['objective'] = 'multi:softprob'
    param['eta'] = 0.1
    param['max_depth'] = 4
    param['silent'] = 1
    param['num_class'] = 3
    param['eval_metric'] = "mlogloss"
    param['min_child_weight'] = 1
    param['subsample'] = 0.8
    param['colsample_bytree'] = 0.8
    param['seed'] = seed_val
    num_rounds = num_rounds

    plst = list(param.items())
    xgtrain = xgb.DMatrix(train_X, label=train_y)

    if test_y is not None:
        xgtest = xgb.DMatrix(test_X, label=test_y)
        watchlist = [ (xgtrain,'train'), (xgtest, 'test') ]
        model = xgb.train(plst, xgtrain, num_rounds, watchlist,\
            early_stopping_rounds=50)
    else:
        xgtest = xgb.DMatrix(test_X)
        watchlist = [ (xgtrain,'train') ]
        model = xgb.train(plst, xgtrain, num_rounds, watchlist)

    pred_test_y = model.predict(xgtest)
    return pred_test_y, model
    
data_path = "../input/"
train_file = data_path + "train.json"
test_file = data_path + "test.json"
train_df = pd.read_json(train_file)
test_df = pd.read_json(test_file)
print(train_df.shape)
print(test_df.shape)

joint = pd.concat([train_df,test_df])

# --------------------------------
# conventional feature engineering
joint["room_dif"] = joint["bedrooms"]-joint["bathrooms"] 
joint["room_sum"] = joint["bedrooms"]+joint["bathrooms"] 
joint["price_per_bed"] = joint["price"]/joint["bedrooms"]
joint["price_per_room"] = joint["price"]/joint["room_sum"]
joint["bed_per_roomsum"] = joint["bedrooms"]/joint["room_sum"]
joint["num_photos"] = joint["photos"].apply(len)
joint["num_features"] = joint["features"].apply(len)
joint["num_description_words"] =\
    joint["description"].apply(lambda x: len(x.split(" ")))

# convert the created column to datetime object so as to extract more features 
joint["created"] = pd.to_datetime(joint["created"])
joint["passed"] = joint["created"] - joint["created"].min()
joint["passed_days"] = joint.passed.dt.days
joint["created_year"] = joint["created"].dt.year
joint["created_month"] = joint["created"].dt.month
joint["created_day"] = joint["created"].dt.day
joint["created_hour"] = joint["created"].dt.hour

# Transform addresses
# joint["street_address"] = joint["street_address"].apply(lambda x:\
#     x.lower().strip())
# joint["display_address"] = joint["display_address"].apply(lambda x:\
#     x.lower().strip())

# --------------------------------
# Adding counts
by_manager = \
    joint[['price','manager_id']].groupby('manager_id').count().reset_index()
by_manager.columns = ['manager_id','listings_by_manager']
joint = pd.merge(joint,by_manager,how='left',on='manager_id')

by_building = \
    joint[['price','building_id']].groupby('building_id').count().reset_index()
by_building.columns = ['building_id','listings_by_building']
joint = pd.merge(joint,by_building,how='left',on='building_id')   

by_display_address = \
    joint[['price','display_address']].groupby('display_address').count().\
    reset_index()
by_display_address.columns = ['display_address','listings_by_display_address']
joint = pd.merge(joint,by_display_address,how='left',on='display_address')

by_street_address = \
    joint[['price','street_address']].groupby('street_address').count().\
    reset_index()
by_street_address.columns = ['street_address','listings_by_street_address']
joint = pd.merge(joint,by_street_address,how='left',on='street_address')

# --- Adding mean of keys
keys = ['price']
mean_features = []
for key in keys:
    by_manager = \
        joint[[key,'manager_id']].groupby('manager_id').mean().reset_index()
    by_manager.columns = ['manager_id',key+'_by_manager']
    joint = pd.merge(joint,by_manager,how='left',on='manager_id')

    by_building = \
        joint[[key,'building_id']].groupby('building_id').mean().reset_index()
    by_building.columns = ['building_id',key+'_by_building']
    joint = pd.merge(joint,by_building,how='left',on='building_id')   

    by_display_address = \
        joint[[key,'display_address']].groupby('display_address').mean().\
        reset_index()
    by_display_address.columns = ['display_address',key+'_by_display_address']
    joint = pd.merge(joint,by_display_address,how='left',on='display_address')

    by_street_address = \
        joint[[key,'street_address']].groupby('street_address').mean().\
        reset_index()
    by_street_address.columns = ['street_address',key+'_by_street_address']
    joint = pd.merge(joint,by_street_address,how='left',on='street_address')

    mean_features += [key+'_by_manager',key+'_by_building',\
        key+'_by_display_address',key+'_by_street_address']
mean_features.remove('price_by_street_address')

# adding price by created day
by_created_day = \
    joint[['listing_id','passed_days']].groupby('passed_days').count().\
    reset_index()
by_created_day.columns = ['passed_days','listings_by_created_day']
joint = pd.merge(joint,by_created_day,how='left',on='passed_days')

# Feature processing for CountVectorizer
joint['features'] =\
    joint["features"].apply(lambda x:\
    " ".join(["_".join(i.split(" ")) for i in x]))

# # Process districts
# ds = joint.description
# joint['isManhattan'] = ds.str.lower().str.contains('manhattan')
# joint['isCentralPark'] = ds.str.lower().str.contains('central park')
# joint['isBroadway'] = ds.str.lower().str.contains('broadway')
# joint['isSoho'] = ds.str.lower().str.contains('soho')
# joint['isMidtown'] = ds.str.lower().str.contains('midtown')
# joint['isChelsea'] = ds.str.lower().str.contains('chelsea')
# joint['isHarlem'] = ds.str.lower().str.contains('harlem')
# joint['isChinatown'] = ds.str.lower().str.contains('chinatown')
# joint['isTribeca'] = ds.str.lower().str.contains('tribeca')
# joint['isLittleItaly'] = ds.str.lower().str.contains('little italy')
# joint['isFlatiron'] = ds.str.lower().str.contains('flatiron')
# joint['isGreenwich'] = ds.str.lower().str.contains('greenwich')
# joint['isBrooklyn'] = ds.str.lower().str.contains('brooklyn')
# joint['isHeights'] = ds.str.lower().str.contains('heights')
# joint['isGramercy'] = ds.str.lower().str.contains('gramercy')
# joint['isMurrayHill'] = ds.str.lower().str.contains('murray hill')
# joint['isFinancialDist'] = ds.str.lower().str.contains('financial district')
# joint['isNolita'] = ds.str.lower().str.contains('nolita')
# joint['isDumbo'] = ds.str.lower().str.contains('dumbo')
# joint['isBatteryPark'] = ds.str.lower().str.contains('battery park')
# ds = 0

# --------------------------------
# Process categorical features
categorical = [\
    "display_address",\
    "manager_id",\
    "building_id",\
    "street_address",
    ]

# Remove entries with one record
# for key in categorical:
#     counts = joint[key].value_counts()
#     joint.ix[joint[key].isin(counts[counts==1].index),key] = "-1"

# Apply LabelEncoder for Hot Encoding to work
joint[categorical] = joint[categorical].apply(LabelEncoder().fit_transform)


'''
===============================
Define features
===============================
'''

# define continuous features - will be untouched
continuous = [\
    "listing_id",\
    "bathrooms", "bedrooms", "latitude", "longitude", "price",\
    "price_per_bed","price_per_room",\
    "num_photos", "num_features","num_description_words",\
    "created_month", "created_day","created_hour",\
    "room_dif","room_sum",\
    "listings_by_building","listings_by_manager",\
    "listings_by_display_address","listings_by_street_address",\
    ]
continuous += mean_features

# # Binary features - merged with continuous
# binary = [\
#     "isManhattan","isCentralPark","isBroadway","isSoho","isMidtown",\
#     "isChelsea","isHarlem","isChinatown","isTribeca","isLittleItaly",\
#     "isFlatiron","isGreenwich","isBrooklyn","isHeights","isGramercy",\
#     "isMurrayHill","isFinancialDist","isNolita","isDumbo","isBatteryPark"
#     ]
# joint[binary] = joint[binary].astype('int')

# Split back
train_df = joint[joint.interest_level.notnull()]
test_df = joint[joint.interest_level.isnull()]

'''
===============================
Define X & y
===============================
'''
target_num_map = {'high':0, 'medium':1, 'low':2}
y = np.array(train_df['interest_level'].apply(lambda x: target_num_map[x]))
X = train_df
X_test = test_df

'''
===============================
Define Pipeline
===============================
'''

# Define pipeline
NO_CHANGE_FIELDS = continuous
CATEGORY_FIELDS = categorical
TEXT_FIELDS = ["features"]
TARGET_AVERAGING_FIELDS = ["manager_id", "price"]

pipeline = Pipeline([
    ('features', FeatureUnion([
        ('continuous', Pipeline([
            ('get', ColumnExtractor(NO_CHANGE_FIELDS)),
            # ('scale', MinMaxScaler()),
            ('debugger', Debugger())
        ])),
        # ('averages', Pipeline([
        #     ('get', ColumnExtractor(TARGET_AVERAGING_FIELDS)),
        #     ('transform', ManagerSkill(threshold = 13)),
        #     ('debugger', Debugger())
        # ])),
        ('categorical', Pipeline([
            ('get', ColumnExtractor(CATEGORY_FIELDS)),
            ('onehot', OneHotEncoder(handle_unknown='ignore')),
            ('debugger', Debugger())
        ])),
        ('vectorizer', Pipeline([
            ('get', ColumnExtractor(TEXT_FIELDS)),
            ('transform', ApartmentFeaturesVectorizer()),
            ('debugger', Debugger())
        ]))
    ]))
])


'''
===============================
XGboost Cycle
===============================
'''
Validation = True

if Validation:
    X_train, X_val, y_train, y_val = train_test_split(X, y, test_size=0.33)
    
    X_train = pipeline.fit_transform(X_train,y_train)
    X_val = pipeline.transform(X_val)

    preds, model = runXGB(X_train,y_train,X_val,y_val)

else:
    X_train = pipeline.fit_transform(X,y)
    X_test = pipeline.transform(X_test)

    preds, model = runXGB(X_train, y, X_test, num_rounds=1000)

    # Prepare Submission
    out_df = pd.DataFrame(preds)
    out_df.columns = ["high", "medium", "low"]
    out_df["listing_id"] = test_df.listing_id.values
    create_submission(model.best_score, out_df, model, None)